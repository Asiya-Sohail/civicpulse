from __future__ import annotations

import duckdb
import pandas as pd
from datetime import timezone

def weekly_seasonal_naive(
    con: duckdb.DuckDBPyConnection,
    cutoff_utc: str,
    lag_hours: int = 168,
) -> pd.DataFrame:
    """
    Evaluate the fixed weekly seasonal-naive baseline.

    Rules:
    - Read only gold.hourly_issue_counts.
    - Use a chronological train/test split.
    - Fill unobserved city/issue/hour combinations with zero.
    - Predict each test value from the same city and issue type
      exactly 168 hours earlier.
    - Do not tune or modify this baseline.
    - Return actual and predicted values for evaluation.
    """
    raw = con.execute(
        """
        SELECT hour_utc, city, issue_type, n_reports
        FROM gold.hourly_issue_counts
        """
    ).df()

    if raw.empty:
        return pd.DataFrame(
            columns=[
                "hour_utc",
                "city",
                "issue_type",
                "actual",
                "prediction",
                "split",
            ]
        )

    raw["hour_utc"] = pd.to_datetime(
        raw["hour_utc"],
        utc=True,
    ).dt.tz_localize(None)

    cutoff = pd.Timestamp(cutoff_utc)

    if cutoff.tzinfo is not None:
        cutoff = cutoff.tz_convert("UTC").tz_localize(None)

    raw["split"] = raw["hour_utc"].apply(
        lambda value: "train" if value < cutoff else "test"
    )
    cities = sorted(raw["city"].dropna().unique())
    issue_types = sorted(raw["issue_type"].dropna().unique())

    hours = pd.date_range(
        start=raw["hour_utc"].min(),
        end=raw["hour_utc"].max(),
        freq="h",
    )

    complete_grid = pd.MultiIndex.from_product(
        [hours, cities, issue_types],
        names=["hour_utc", "city", "issue_type"],
    ).to_frame(index=False)

    observed = raw[
        ["hour_utc", "city", "issue_type", "n_reports"]
    ]

    panel = complete_grid.merge(
        observed,
        on=["hour_utc", "city", "issue_type"],
        how="left",
    )

    panel["n_reports"] = (
        panel["n_reports"]
        .fillna(0)
        .astype(int)
    )

    panel["split"] = panel["hour_utc"].apply(
        lambda value: "train" if value < cutoff else "test"
    )
    panel["lag_hour_utc"] = (
        panel["hour_utc"]
        - pd.Timedelta(hours=lag_hours)
    )

    lag_values = panel[
        [
            "hour_utc",
            "city",
            "issue_type",
            "n_reports",
        ]
    ].rename(
        columns={
            "hour_utc": "lag_hour_utc",
            "n_reports": "lag_reports",
        }
    )

    panel = panel.merge(
        lag_values,
        on=["lag_hour_utc", "city", "issue_type"],
        how="left",
    )

    panel["prediction"] = (
        panel["lag_reports"]
        .fillna(0)
        .astype(int)
    )

    result = panel[
        [
            "hour_utc",
            "city",
            "issue_type",
            "n_reports",
            "prediction",
            "split",
        ]
    ].rename(
        columns={"n_reports": "actual"}
    )

    return result
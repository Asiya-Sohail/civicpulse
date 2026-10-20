from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd


def fit_explainable_poisson_model(
    con: duckdb.DuckDBPyConnection,
    cutoff_utc: str,
    alpha: float,
) -> Any:
    """
    Fit an explainable Poisson regression using training data only.

    Required constraints:
    - Read Gold data only.
    - Exclude rows at or after cutoff_utc.
    - Use city, issue type, calendar features, and approved weather features.
    - Do not use the chronological test period during fitting.
    - Return the fitted model and preprocessing information.
    """
def build_model_frame(
    con: duckdb.DuckDBPyConnection,
    cutoff_utc: str,
) -> pd.DataFrame:
    """
    Load the Gold modeling frame.

    Required behavior:
    - Read only gold.hourly_issue_counts_with_weather.
    - Keep n_reports as the target.
    - Create calendar features from hour_utc.
    - Keep city and issue_type as categorical fields.
    - Preserve weather NULLs until preprocessing.
    - Add a train/test split using cutoff_utc.
    - Never use test rows to fit preprocessing or the model.
    """
    
    frame = con.execute(
        """
        SELECT
            hour_utc,
            city,
            issue_type,
            n_reports,
            temperature_2m,
            precipitation,
            wind_speed_10m
        FROM gold.hourly_issue_counts_with_weather
        ORDER BY hour_utc, city, issue_type
        """
    ).df()

    frame["hour_utc"] = pd.to_datetime(
        frame["hour_utc"],
        utc=True,
    )

    cutoff = pd.Timestamp(cutoff_utc)

    if cutoff.tzinfo is None:
        cutoff = cutoff.tz_localize("UTC")
    else:
        cutoff = cutoff.tz_convert("UTC")

    frame["hour_of_day"] = frame["hour_utc"].dt.hour
    frame["day_of_week"] = frame["hour_utc"].dt.dayofweek

    frame["split"] = "test"
    frame.loc[
        frame["hour_utc"] < cutoff,
        "split",
    ] = "train"

    return frame
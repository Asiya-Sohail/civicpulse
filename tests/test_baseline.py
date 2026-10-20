import duckdb
import pandas as pd

from civicpulse.modeling.baseline import weekly_seasonal_naive


def test_weekly_seasonal_naive_uses_168_hour_lag(tmp_path):
    con = duckdb.connect(str(tmp_path / "baseline.duckdb"))

    con.execute("CREATE SCHEMA gold")

    con.execute(
        """
        CREATE TABLE gold.hourly_issue_counts (
            hour_utc TIMESTAMP,
            city VARCHAR,
            issue_type VARCHAR,
            n_reports INTEGER
        )
        """
    )

    con.execute(
        """
        INSERT INTO gold.hourly_issue_counts
        VALUES
            ('2026-04-01 00:00:00', 'Karachi', 'Electricity', 2),
            ('2026-04-08 00:00:00', 'Karachi', 'Electricity', 3),
            ('2026-04-01 00:00:00', 'Lahore', 'Water', 1),
            ('2026-04-08 00:00:00', 'Lahore', 'Water', 4)
        """
    )

    result = weekly_seasonal_naive(
        con=con,
        cutoff_utc="2026-04-06T18:00:00Z",
    )

    test_rows = result[result["split"] == "test"].sort_values(
        ["city", "issue_type", "hour_utc"]
    )

    assert len(test_rows) == 124

    karachi = test_rows[
        (test_rows["city"] == "Karachi")
        & (test_rows["issue_type"] == "Electricity")
        & (
            test_rows["hour_utc"]
            == pd.Timestamp("2026-04-08 00:00:00")
        )
    ].iloc[0]

    lahore = test_rows[
        (test_rows["city"] == "Lahore")
        & (test_rows["issue_type"] == "Water")
        & (
            test_rows["hour_utc"]
            == pd.Timestamp("2026-04-08 00:00:00")
        )
    ].iloc[0]

    assert karachi["actual"] == 3
    assert karachi["prediction"] == 2

    assert lahore["actual"] == 4
    assert lahore["prediction"] == 1

    assert (test_rows["actual"] == 0).any()
    assert (test_rows["prediction"] == 0).any()
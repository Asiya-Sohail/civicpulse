import duckdb

from civicpulse.modeling.poisson_model import build_model_frame


def test_build_model_frame_uses_cutoff_and_gold_weather_table(tmp_path):
    con = duckdb.connect(str(tmp_path / "model.duckdb"))

    con.execute("CREATE SCHEMA gold")

    con.execute(
        """
        CREATE TABLE gold.hourly_issue_counts_with_weather (
            hour_utc TIMESTAMP,
            city VARCHAR,
            issue_type VARCHAR,
            n_reports BIGINT,
            temperature_2m DOUBLE,
            precipitation DOUBLE,
            wind_speed_10m DOUBLE
        )
        """
    )

    con.execute(
        """
        INSERT INTO gold.hourly_issue_counts_with_weather
        VALUES
            ('2026-04-06 17:00:00', 'Karachi', 'Electricity', 2, 30.0, 0.0, 12.0),
            ('2026-04-06 18:00:00', 'Karachi', 'Electricity', 3, 31.0, 0.0, 13.0)
        """
    )

    result = build_model_frame(
        con=con,
        cutoff_utc="2026-04-06T18:00:00Z",
    )

    assert len(result) == 2
    assert set(result["split"]) == {"train", "test"}
    assert "hour_of_day" in result.columns
    assert "day_of_week" in result.columns
    assert "n_reports" in result.columns
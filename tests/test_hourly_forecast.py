from __future__ import annotations

import duckdb
import numpy as np

from civicpulse.modeling import forecast


class _IdentityPreprocessor:
    def transform(self, frame):
        return np.zeros((len(frame), 1))


class _FixedModel:
    def predict(self, features):
        return np.full(len(features), 2.0)


def test_hourly_forecast_saves_future_poisson_rows_without_network(monkeypatch):
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA gold")
    con.execute(
        """CREATE TABLE gold.hourly_issue_counts_with_weather (
            hour_utc TIMESTAMP, city VARCHAR, issue_type VARCHAR,
            temperature_2m DOUBLE, precipitation DOUBLE, wind_speed_10m DOUBLE
        )"""
    )
    con.execute(
        "INSERT INTO gold.hourly_issue_counts_with_weather VALUES "
        "('2026-10-20 00:00:00', 'Karachi', 'Water', 28, 0, 15)"
    )
    monkeypatch.setattr(
        forecast.poisson_model,
        "select_poisson_alpha",
        lambda con, cutoff_utc: {"selected_alpha": 0.5},
    )
    monkeypatch.setattr(
        forecast.poisson_model,
        "fit_explainable_poisson_model",
        lambda con, cutoff_utc, alpha: {
            "preprocessor": _IdentityPreprocessor(),
            "model": _FixedModel(),
            "feature_columns": ["hour_of_day"],
            "training_rows": 42,
        },
    )
    monkeypatch.setattr(forecast, "_fetch_forecast_weather", lambda city: ({}, None))

    result = forecast.generate_hourly_forecasts(con, horizon_hours=2)

    rows = con.execute(
        """SELECT forecast_run_id, city, issue_type, expected_reports,
                  lower_95, upper_95, training_rows, weather_source_url
           FROM gold.poisson_forecasts ORDER BY forecast_for_utc"""
    ).fetchall()
    assert result["forecast_rows"] == 2
    assert len(rows) == 2
    assert all(row[0] == result["forecast_run_id"] for row in rows)
    assert all(row[1:3] == ("Karachi", "Water") for row in rows)
    assert all(row[3] == 2.0 and row[6] == 42 and row[7] is None for row in rows)
    assert all(row[4] <= 2 <= row[5] for row in rows)
    con.close()

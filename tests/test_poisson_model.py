import duckdb
import pandas as pd

from civicpulse.modeling.poisson_model import (
    build_model_frame,
    evaluate_poisson_model,
    fit_explainable_poisson_model,
    make_poisson_preprocessor,
    select_poisson_alpha,
)


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
def test_poisson_preprocessor_handles_categories_and_missing_weather():
    frame = pd.DataFrame(
        {
            "city": ["Karachi", "Lahore"],
            "issue_type": ["Electricity", "Water"],
            "hour_of_day": [10, 11],
            "day_of_week": [1, 2],
            "temperature_2m": [30.0, None],
            "precipitation": [0.0, 1.0],
            "wind_speed_10m": [12.0, None],
        }
    )

    preprocessor = make_poisson_preprocessor()
    transformed = preprocessor.fit_transform(frame)

    assert transformed.shape[0] == 2
    assert transformed.shape[1] > 0
    assert not pd.isna(transformed).any()
def test_poisson_model_fit_contract_uses_training_period_only(tmp_path):
    con = duckdb.connect(str(tmp_path / "fit.duckdb"))

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
            ('2026-04-05 00:00:00', 'Karachi', 'Electricity', 1, 30.0, 0.0, 10.0),
            ('2026-04-05 01:00:00', 'Karachi', 'Electricity', 2, 31.0, 0.0, 11.0),
            ('2026-04-06 00:00:00', 'Lahore', 'Water', 1, 25.0, 0.5, 8.0),
            ('2026-04-08 00:00:00', 'Karachi', 'Electricity', 3, 32.0, 0.0, 12.0)
        """
    )

    result = fit_explainable_poisson_model(
        con=con,
        cutoff_utc="2026-04-06T18:00:00Z",
        alpha=1.0,
    )

    assert isinstance(result, dict)
    assert result["training_rows"] == 3
    assert "model" in result
    assert "preprocessor" in result
def test_select_poisson_alpha_returns_training_only_results(tmp_path):
    con = duckdb.connect(str(tmp_path / "alpha.duckdb"))

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
            ('2026-04-01 00:00:00', 'Karachi', 'Electricity', 1, 30.0, 0.0, 10.0),
            ('2026-04-02 00:00:00', 'Karachi', 'Electricity', 2, 30.0, 0.0, 10.0),
            ('2026-04-03 00:00:00', 'Lahore', 'Water', 1, 25.0, 0.5, 8.0),
            ('2026-04-04 00:00:00', 'Lahore', 'Water', 2, 25.0, 0.5, 8.0),
            ('2026-04-05 00:00:00', 'Karachi', 'Electricity', 3, 31.0, 0.0, 11.0),
            ('2026-04-08 00:00:00', 'Karachi', 'Electricity', 9, 32.0, 0.0, 12.0)
        """
    )

    result = select_poisson_alpha(
        con=con,
        cutoff_utc="2026-04-06T18:00:00Z",
        alpha_grid=(0.1, 1.0),
    )

    assert isinstance(result, dict)
    assert result["selected_alpha"] in (0.1, 1.0)
    assert len(result["validation_results"]) == 2
    assert result["training_rows"] == 5
def test_evaluate_poisson_model_uses_locked_test_period(tmp_path):
    con = duckdb.connect(str(tmp_path / "evaluation.duckdb"))

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
            ('2026-04-01 00:00:00', 'Karachi', 'Electricity', 1, 30.0, 0.0, 10.0),
            ('2026-04-02 00:00:00', 'Karachi', 'Electricity', 2, 30.0, 0.0, 10.0),
            ('2026-04-03 00:00:00', 'Lahore', 'Water', 1, 25.0, 0.5, 8.0),
            ('2026-04-04 00:00:00', 'Lahore', 'Water', 2, 25.0, 0.5, 8.0),
            ('2026-04-08 00:00:00', 'Karachi', 'Electricity', 3, 32.0, 0.0, 12.0)
        """
    )

    result = evaluate_poisson_model(
        con=con,
        cutoff_utc="2026-04-06T18:00:00Z",
        alpha=1.0,
    )

    assert isinstance(result, dict)
    assert result["test_rows"] == 1
    assert result["mae"] >= 0
    assert len(result["predictions"]) == 1
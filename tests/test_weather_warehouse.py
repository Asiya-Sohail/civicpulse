import json

import duckdb

from civicpulse.ingest import manifest
from civicpulse.warehouse.weather import (
    build_weather_table,
    join_weather_features_to_gold,
)


def test_weather_table_contract(tmp_path):
    bronze_dir = tmp_path / "bronze" / "open_meteo"
    bronze_dir.mkdir(parents=True)

    raw_path = bronze_dir / (
        "open_meteo_karachi_2024-10-08_2024-10-08.json"
    )

    payload = {
        "hourly": {
            "time": ["2024-10-08T00:00"],
            "temperature_2m": [25.0],
            "precipitation": [0.0],
            "wind_speed_10m": [10.0],
        }
    }

    raw_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    manifest_path = tmp_path / "manifest.csv"

    manifest.append_manifest(
        [
            {
                "file_path": str(raw_path),
                "source": "open_meteo",
                "source_url": (
                    "https://archive-api.open-meteo.com/v1/archive"
                ),
                "search_query": "latitude=24.86&longitude=67.01",
                "fetch_timestamp_utc": "2026-10-20T00:00:00Z",
                "sha256": manifest.sha256_file(raw_path),
                "bytes": raw_path.stat().st_size,
                "license_terms": "test Open-Meteo licence",
            }
        ],
        manifest_path,
    )

    con = duckdb.connect(str(tmp_path / "weather.duckdb"))

    result = build_weather_table(
        con=con,
        bronze_dir=tmp_path / "bronze",
        manifest_path=manifest_path,
    )

    assert isinstance(result, dict)
    assert result["rows"] == 1

    row = con.execute(
        """
        SELECT city, hour_utc, temperature_2m,
               precipitation, wind_speed_10m
        FROM gold.weather_hourly_features
        """
    ).fetchone()

    assert row[0] == "Karachi"
    assert str(row[1]) == "2024-10-08 00:00:00"
    assert row[2:] == (25.0, 0.0, 10.0)


def test_weather_join_preserves_gold_rows(tmp_path):
    con = duckdb.connect(str(tmp_path / "join.duckdb"))

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
            ('2024-10-08 00:00:00', 'Karachi', 'Electricity', 3),
            ('2024-10-08 01:00:00', 'Lahore', 'Water', 2)
        """
    )

    con.execute(
        """
        CREATE TABLE gold.weather_hourly_features (
            city VARCHAR,
            hour_utc TIMESTAMP,
            temperature_2m DOUBLE,
            precipitation DOUBLE,
            wind_speed_10m DOUBLE
        )
        """
    )

    con.execute(
        """
        INSERT INTO gold.weather_hourly_features
        VALUES ('Karachi', '2024-10-08 00:00:00', 25.0, 0.0, 10.0)
        """
    )

    join_weather_features_to_gold(con)

    rows = con.execute(
        """
        SELECT city, issue_type, n_reports,
               temperature_2m, precipitation, wind_speed_10m
        FROM gold.hourly_issue_counts_with_weather
        ORDER BY hour_utc
        """
    ).fetchall()

    assert len(rows) == 2

    assert rows[0] == (
        "Karachi",
        "Electricity",
        3,
        25.0,
        0.0,
        10.0,
    )

    assert rows[1] == (
        "Lahore",
        "Water",
        2,
        None,
        None,
        None,
    )
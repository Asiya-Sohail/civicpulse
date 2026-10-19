from __future__ import annotations

from pathlib import Path

import duckdb
import json
import hashlib

from civicpulse.ingest.open_meteo import validate_weather_payload
from datetime import datetime, timezone
from civicpulse import config
from civicpulse.ingest import manifest


def build_weather_table(
    con: duckdb.DuckDBPyConnection,
    bronze_dir: Path = config.BRONZE_DIR,
    manifest_path: Path = config.MANIFEST_PATH,
) -> dict[str, int]:
    """
    Build a warehouse weather table from validated Open-Meteo Bronze files.

    Required behavior:
    - Read only files recorded in the manifest.
    - Preserve manifest ID, source URL, fetch time, SHA-256, and licence.
    - Keep missing weather values as NULL.
    - Parse timestamps as UTC.
    - Store city and hourly weather variables.
    """
    weather_dir = bronze_dir / "open_meteo"
    lookup = manifest.manifest_lookup(manifest_path)
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")

    con.execute(
        """
        CREATE OR REPLACE TABLE gold.weather_hourly_features (
            city VARCHAR,
            hour_utc TIMESTAMP,
            temperature_2m DOUBLE,
            precipitation DOUBLE,
            wind_speed_10m DOUBLE,
            manifest_id VARCHAR,
            source_url VARCHAR,
            fetch_timestamp_utc TIMESTAMP,
            sha256 VARCHAR,
            license_terms VARCHAR
        )
        """
    )
    raw_paths = sorted(weather_dir.glob("*.json"))
    rows = []

    for raw_path in raw_paths:
        metadata = lookup.get(str(raw_path.resolve()))

        if metadata is None:
            raise RuntimeError(
                f"Weather file is missing from the manifest: {raw_path}"
            )

        actual_sha256 = manifest.sha256_file(raw_path)

        if actual_sha256 != metadata["sha256"]:
            raise manifest.ManifestIntegrityError(
                f"SHA-256 mismatch for weather file: {raw_path}"
            )

        payload = json.loads(
            raw_path.read_text(encoding="utf-8")
        )

        validate_weather_payload(payload)
        hourly = payload["hourly"]

        city_token = (
            raw_path.stem
            .removeprefix("open_meteo_")
            .rsplit("_", 2)[0]
        )
        city = city_token.replace("_", " ").title()

        manifest_id = hashlib.sha256(
            f"{metadata['file_path']}|{metadata['sha256']}".encode()
        ).hexdigest()

        fetch_timestamp = datetime.fromisoformat(
            metadata["fetch_timestamp_utc"].replace("Z", "+00:00")
        ).astimezone(timezone.utc).replace(tzinfo=None)

        for index, timestamp in enumerate(hourly["time"]):
            parsed_timestamp = datetime.fromisoformat(
                str(timestamp).replace("Z", "+00:00")
            )

            if parsed_timestamp.tzinfo is None:
                parsed_timestamp = parsed_timestamp.replace(
                    tzinfo=timezone.utc
                )

            rows.append(
                {
                    "city": city,
                    "hour_utc": parsed_timestamp.astimezone(
                        timezone.utc
                    ).replace(tzinfo=None),
                    "temperature_2m": hourly["temperature_2m"][index],
                    "precipitation": hourly["precipitation"][index],
                    "wind_speed_10m": hourly["wind_speed_10m"][index],
                    "manifest_id": manifest_id,
                    "source_url": metadata["source_url"],
                    "fetch_timestamp_utc": fetch_timestamp,
                    "sha256": metadata["sha256"],
                    "license_terms": metadata["license_terms"],
                }
            )
    if rows:
        con.executemany(
            """
            INSERT INTO gold.weather_hourly_features (
                city,
                hour_utc,
                temperature_2m,
                precipitation,
                wind_speed_10m,
                manifest_id,
                source_url,
                fetch_timestamp_utc,
                sha256,
                license_terms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["city"],
                    row["hour_utc"],
                    row["temperature_2m"],
                    row["precipitation"],
                    row["wind_speed_10m"],
                    row["manifest_id"],
                    row["source_url"],
                    row["fetch_timestamp_utc"],
                    row["sha256"],
                    row["license_terms"],
                )
                for row in rows
            ],
        )

    return {"rows": len(rows)}


def join_weather_features_to_gold(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """
    Join weather features to Gold hourly counts by UTC hour and city.

    Required behavior:
    - Do not change civic-event counts.
    - Preserve rows with no matching weather observation.
    - Keep weather fields separate from the event target.
    """
    con.execute(
        """
        CREATE OR REPLACE TABLE gold.hourly_issue_counts_with_weather AS
        SELECT
            g.*,
            w.temperature_2m,
            w.precipitation,
            w.wind_speed_10m
        FROM gold.hourly_issue_counts AS g
        LEFT JOIN gold.weather_hourly_features AS w
          ON g.hour_utc = w.hour_utc
         AND g.city = w.city
        """
    )

    return None
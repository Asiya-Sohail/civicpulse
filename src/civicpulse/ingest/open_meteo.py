from __future__ import annotations
from urllib.parse import urlencode
from civicpulse.ingest import manifest

import json
from datetime import date, datetime, timezone
from pathlib import Path


import requests

from civicpulse import config


def fetch_historical_weather(
    start_date: date,
    end_date: date,
    city: str,
    latitude: float,
    longitude: float,
    output_dir: Path,
    manifest_path: Path = config.MANIFEST_PATH,
) -> Path:
    """
    Fetch hourly Open-Meteo historical data for one city.

    The manifest-recording step is still pending.
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": ",".join(config.OPEN_METEO_HOURLY_VARIABLES),
        "timezone": config.OPEN_METEO_TIMEZONE,
    }

    response = requests.get(
        config.OPEN_METEO_ARCHIVE_URL,
        params=params,
        timeout=(10, 60),
    )

    response.raise_for_status()

    payload = response.json()
    validate_weather_payload(payload)

    output_dir.mkdir(parents=True, exist_ok=True)

    safe_city = city.lower().replace(" ", "_")

    raw_path = output_dir / (
        f"open_meteo_{safe_city}_{start_date.isoformat()}_"
        f"{end_date.isoformat()}.json"
    )

    if raw_path.exists():
        raise FileExistsError(
            f"Raw weather file already exists: {raw_path}"
        )

    raw_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # TODO: record manifest metadata before finalizing this function.
    manifest.make_read_only(raw_path)

    manifest_row = {
        "file_path": manifest.rel_path(raw_path),
        "source": "open_meteo",
        "source_url": response.url,
        "search_query": urlencode(params),
        "fetch_timestamp_utc": manifest.utc_now_iso(),
        "sha256": manifest.sha256_file(raw_path),
        "bytes": raw_path.stat().st_size,
        "license_terms": config.OPEN_METEO_LICENSE,
    }

    manifest.append_manifest([manifest_row], manifest_path)

    return raw_path
    return raw_path


def validate_weather_payload(payload: dict) -> None:
    """
    Validate the required Open-Meteo response structure.

    Required checks:
    - hourly.time exists.
    - Each requested variable exists.
    - All hourly arrays have equal length.
    - UTC timestamps are parseable.
    - Missing weather values remain missing and are not silently changed to zero.
    """
    # TODO: implement validation checks.
    if not isinstance(payload, dict):
        raise TypeError("Weather payload must be a dictionary.")

    hourly = payload.get("hourly")

    if not isinstance(hourly, dict):
        raise ValueError("Weather payload is missing the hourly object.")

    required_fields = [
        "time",
        "temperature_2m",
        "precipitation",
        "wind_speed_10m",
    ]
    missing_fields = [
        field for field in required_fields
        if field not in hourly
    ]

    if missing_fields:
        raise ValueError(
            f"Weather payload is missing fields: {missing_fields}"
        )
    for field in required_fields:
        if not isinstance(hourly[field], list):
            raise TypeError(
                f"Weather field '{field}' must be a list."
            )
    lengths = {
        field: len(hourly[field])
        for field in required_fields
    }

    if len(set(lengths.values())) != 1:
        raise ValueError(
            f"Weather fields have unequal lengths: {lengths}"
        )

    for timestamp in hourly["time"]:
        try:
            parsed_timestamp = datetime.fromisoformat(
                str(timestamp).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError(
                f"Invalid UTC timestamp: {timestamp}"
            ) from error

        if parsed_timestamp.tzinfo is None:
            parsed_timestamp = parsed_timestamp.replace(
                tzinfo=timezone.utc
            )

        parsed_timestamp.astimezone(timezone.utc)
    return None
from __future__ import annotations

from pathlib import Path

import duckdb

from civicpulse import config


def build_weather_table(
    con: duckdb.DuckDBPyConnection,
    bronze_dir: Path = config.OPEN_METEO_BRONZE_DIR,
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
    raise NotImplementedError


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
    raise NotImplementedError
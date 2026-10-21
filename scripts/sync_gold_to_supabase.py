"""Upsert CivicPulse Gold tables from the local DuckDB warehouse to Supabase.

Run after `python -m civicpulse data` and (if you want forecast rows)
`python -m civicpulse forecast-model`. Tables must already exist in Supabase.
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import requests
import duckdb

# Import config to load the project-root .env and find the DuckDB database.
from civicpulse import config  # noqa: E402


TABLES = (
    ("issue_events", "row_id", True),
    ("hourly_issue_counts", "row_id", True),
    ("weather_hourly_features", "city,hour_utc,manifest_id", False),
    ("poisson_forecasts", "forecast_run_id,forecast_for_utc,city,issue_type", False),
    ("data_freshness", "source", False),
)
UTC_FIELDS = {
    "published_utc", "crawled_utc", "manifest_fetch_utc", "hour_utc",
    "fetch_timestamp_utc", "generated_at_utc", "forecast_for_utc",
    "training_cutoff_utc", "last_fetch_utc", "last_published_utc", "checked_utc",
}
PKT_FIELDS = {"published_pkt_display", "hour_pkt_display"}


def _serialize(value: Any, column: str) -> Any:
    """Convert DuckDB values to JSON, explicitly tagging naive timestamps with their timezone."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.isoformat()
        if column in UTC_FIELDS:
            return value.isoformat() + "+00:00"
        if column in PKT_FIELDS:
            return value.isoformat() + "+05:00"
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    # DuckDB's scalar values (str, int, bool) are JSON-compatible.
    return value


def _upsert_table(
    session: requests.Session,
    base_url: str,
    api_key: str,
    table: str,
    conflict_columns: str,
    required: bool,
    batch_size: int,
) -> int:
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    try:
        exists = con.execute(
            """SELECT count(*) FROM information_schema.tables
               WHERE table_schema='gold' AND table_name=? AND table_type='BASE TABLE'""",
            [table],
        ).fetchone()[0]
        if not exists:
            if required:
                raise RuntimeError(f"Required DuckDB Gold table is missing: gold.{table}. Run `python -m civicpulse data` first.")
            print(f"Skipping gold.{table}: it does not exist in DuckDB yet.")
            return 0

        cursor = con.execute(f'SELECT * FROM gold."{table}"')
        columns = [description[0] for description in cursor.description]
        endpoint = f"{base_url}/rest/v1/{quote(table)}"
        uploaded = 0
        while rows := cursor.fetchmany(batch_size):
            records = [
                {column: _serialize(value, column) for column, value in zip(columns, row)}
                for row in rows
            ]
            response = session.post(
                endpoint,
                params={"on_conflict": conflict_columns},
                headers={
                    "apikey": api_key,
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                    "Content-Type": "application/json",
                },
                data=json.dumps(records, allow_nan=False),
                timeout=90,
            )
            if not response.ok:
                detail = response.text[:1500]
                raise RuntimeError(
                    f"Supabase upsert failed for {table} (HTTP {response.status_code}): {detail}"
                )
            uploaded += len(records)
            print(f"  {table}: upserted {uploaded:,} rows")
        return uploaded
    finally:
        con.close()


def main() -> int:
    base_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    api_key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if not base_url or not api_key:
        print("Set SUPABASE_URL and SUPABASE_SECRET_KEY in your private project .env file.", file=sys.stderr)
        return 2
    try:
        batch_size = int(os.getenv("SUPABASE_SYNC_BATCH_SIZE", "500"))
    except ValueError:
        print("SUPABASE_SYNC_BATCH_SIZE must be an integer.", file=sys.stderr)
        return 2
    if batch_size < 1 or batch_size > 1000:
        print("SUPABASE_SYNC_BATCH_SIZE must be between 1 and 1000.", file=sys.stderr)
        return 2

    session = requests.Session()
    total = 0
    try:
        print(f"Reading DuckDB Gold data from: {config.DB_PATH}")
        for table, conflict, required in TABLES:
            print(f"Syncing gold.{table}...")
            count = _upsert_table(session, base_url, api_key, table, conflict, required, batch_size)
            total += count
        print(f"Supabase Gold sync complete: {total:,} rows upserted.")
        return 0
    except (requests.RequestException, RuntimeError) as exc:
        print(f"Supabase Gold sync failed: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

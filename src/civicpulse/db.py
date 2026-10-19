"""DuckDB connection + the two tables that live across rebuilds (quality_log, pipeline_runs)."""
from __future__ import annotations

from pathlib import Path

import duckdb

from civicpulse import config


def connect(db_path: Path | str = config.DB_PATH) -> duckdb.DuckDBPyConnection:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    ensure_meta(con)
    return con


def ensure_meta(con: duckdb.DuckDBPyConnection) -> None:
    for schema in ("bronze", "silver", "gold"):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS quality_log (
            run_id VARCHAR, logged_at_utc TIMESTAMP, layer VARCHAR, table_name VARCHAR,
            check_name VARCHAR, action VARCHAR, rows_affected BIGINT, detail VARCHAR
        )"""
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id VARCHAR, started_utc TIMESTAMP, finished_utc TIMESTAMP, status VARCHAR,
            bronze_freenewsapi BIGINT, bronze_gdelt BIGINT, silver_rows BIGINT,
            quarantined_rows BIGINT, gold_events BIGINT, gold_hourly BIGINT, error VARCHAR
        )"""
    )
    old_column = con.execute(
        """SELECT count(*) FROM information_schema.columns
           WHERE table_name='pipeline_runs' AND column_name='bronze_newsagent'"""
    ).fetchone()[0]
    new_column = con.execute(
        """SELECT count(*) FROM information_schema.columns
           WHERE table_name='pipeline_runs' AND column_name='bronze_freenewsapi'"""
    ).fetchone()[0]
    # Existing local warehouses keep their run history under the old source-column name.
    if old_column and not new_column:
        con.execute("ALTER TABLE pipeline_runs RENAME COLUMN bronze_newsagent TO bronze_freenewsapi")

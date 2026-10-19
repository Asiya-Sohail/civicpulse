"""Bronze: raw records exactly as received, plus fetch metadata. Rebuilt from files on disk."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import pandas as pd

from civicpulse import config
from civicpulse.ingest import gdelt, manifest
from civicpulse.ingest.freenewsapi import extract_items


def _fetched(meta: dict | None, path: Path) -> pd.Timestamp:
    if meta and meta.get("fetch_timestamp_utc"):
        return pd.Timestamp(meta["fetch_timestamp_utc"]).tz_localize(None)
    return pd.Timestamp(path.stat().st_mtime, unit="s")


def build_bronze(
    con: duckdb.DuckDBPyConnection,
    bronze_dir: Path = config.BRONZE_DIR,
    cache_dir: Path = config.CACHE_DIR,
    manifest_path: Path = config.MANIFEST_PATH,
) -> dict[str, int]:
    lookup = manifest.validate_bronze_inventory(bronze_dir, manifest_path)

    # ---- FreeNewsAPI -----------------------------------------------------------------
    con.execute("DROP TABLE IF EXISTS bronze.freenewsapi_raw")
    con.execute(
        """CREATE TABLE bronze.freenewsapi_raw (
               raw_id VARCHAR, file_path VARCHAR, file_name VARCHAR, file_sha256 VARCHAR,
               manifest_id VARCHAR, manifest_source VARCHAR, source_url VARCHAR,
               fetched_utc TIMESTAMP, manifest_fetch_utc TIMESTAMP, query VARCHAR,
               license_terms VARCHAR, raw_json VARCHAR)"""
    )
    rows = []
    for path in sorted((bronze_dir / "freenewsapi").glob("*.json")):
        meta = lookup[str(path.resolve())]
        sha = meta["sha256"]
        manifest_id = hashlib.sha256(
            f"{meta['file_path']}|{sha}".encode()
        ).hexdigest()
        items = extract_items(json.loads(path.read_text(encoding="utf-8")))
        fetched = _fetched(meta, path)
        for i, item in enumerate(items):
            rows.append({
                "raw_id": hashlib.sha256(f"{sha}:{i}".encode()).hexdigest()[:16],
                "file_path": meta["file_path"], "file_name": path.name, "file_sha256": sha,
                "manifest_id": manifest_id, "manifest_source": meta["source"],
                "source_url": meta["source_url"], "fetched_utc": fetched,
                "manifest_fetch_utc": _fetched(meta, path), "query": meta["search_query"],
                "license_terms": meta["license_terms"],
                "raw_json": json.dumps(item, ensure_ascii=False),
            })
    if rows:
        df = pd.DataFrame(rows)
        con.execute("INSERT INTO bronze.freenewsapi_raw SELECT * FROM df")

    # ---- GDELT -----------------------------------------------------------------------
    con.execute("DROP TABLE IF EXISTS bronze.gdelt_events_raw")
    cols = ", ".join(f'"{c}" VARCHAR' for c in gdelt.GDELT_COLS)
    con.execute(
        f"""CREATE TABLE bronze.gdelt_events_raw ({cols},
            file_path VARCHAR, file_name VARCHAR, file_sha256 VARCHAR, manifest_id VARCHAR,
            manifest_source VARCHAR, source_url VARCHAR, fetched_utc TIMESTAMP,
            manifest_fetch_utc TIMESTAMP, search_query VARCHAR, license_terms VARCHAR)"""
    )
    batch: list[pd.DataFrame] = []

    def flush() -> None:
        if batch:
            big = pd.concat(batch, ignore_index=True)
            con.execute("INSERT INTO bronze.gdelt_events_raw BY NAME SELECT * FROM big")
            batch.clear()

    for zpath in sorted((bronze_dir / "gdelt").glob("*.export.CSV.zip")):
        meta = lookup[str(zpath.resolve())]
        df = gdelt.parse_zip_cached(zpath, cache_dir, meta["sha256"])
        if df.empty:
            continue
        df = df.copy()
        df["file_path"] = meta["file_path"]
        df["file_name"] = zpath.name
        df["file_sha256"] = meta["sha256"]
        df["manifest_id"] = hashlib.sha256(
            f"{meta['file_path']}|{meta['sha256']}".encode()
        ).hexdigest()
        df["manifest_source"] = meta["source"]
        df["source_url"] = meta["source_url"]
        df["fetched_utc"] = _fetched(meta, zpath)
        df["manifest_fetch_utc"] = _fetched(meta, zpath)
        df["search_query"] = meta["search_query"]
        df["license_terms"] = meta["license_terms"]
        batch.append(df)
        if sum(len(b) for b in batch) > 200_000:
            flush()
    flush()

    return {
        "freenewsapi": con.execute("SELECT count(*) FROM bronze.freenewsapi_raw").fetchone()[0],
        "gdelt": con.execute("SELECT count(*) FROM bronze.gdelt_events_raw").fetchone()[0],
    }

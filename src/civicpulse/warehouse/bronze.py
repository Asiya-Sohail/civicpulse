"""Bronze: raw records exactly as received, plus fetch metadata. Rebuilt from files on disk."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import pandas as pd

from civicpulse import config
from civicpulse.ingest import gdelt, manifest
from civicpulse.ingest.newsagent import extract_items


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
    lookup = manifest.manifest_lookup(manifest_path)

    # ---- NewsAgent -------------------------------------------------------------------
    con.execute("DROP TABLE IF EXISTS bronze.newsagent_raw")
    con.execute(
        """CREATE TABLE bronze.newsagent_raw (
               raw_id VARCHAR, file_name VARCHAR, file_sha256 VARCHAR,
               fetched_utc TIMESTAMP, query VARCHAR, raw_json VARCHAR)"""
    )
    rows = []
    for path in sorted((bronze_dir / "newsagent").glob("*.json")):
        meta = lookup.get(path.name)
        sha = (meta or {}).get("sha256") or manifest.sha256_file(path)
        items = extract_items(json.loads(path.read_text(encoding="utf-8")))
        fetched = _fetched(meta, path)
        for i, item in enumerate(items):
            rows.append({
                "raw_id": hashlib.sha256(f"{sha}:{i}".encode()).hexdigest()[:16],
                "file_name": path.name, "file_sha256": sha, "fetched_utc": fetched,
                "query": (meta or {}).get("search_query", ""),
                "raw_json": json.dumps(item, ensure_ascii=False),
            })
    if rows:
        df = pd.DataFrame(rows)
        con.execute("INSERT INTO bronze.newsagent_raw SELECT * FROM df")

    # ---- GDELT -----------------------------------------------------------------------
    con.execute("DROP TABLE IF EXISTS bronze.gdelt_events_raw")
    cols = ", ".join(f'"{c}" VARCHAR' for c in gdelt.GDELT_COLS)
    con.execute(
        f"""CREATE TABLE bronze.gdelt_events_raw ({cols},
            file_name VARCHAR, file_sha256 VARCHAR, fetched_utc TIMESTAMP)"""
    )
    batch: list[pd.DataFrame] = []

    def flush() -> None:
        if batch:
            big = pd.concat(batch, ignore_index=True)
            con.execute("INSERT INTO bronze.gdelt_events_raw BY NAME SELECT * FROM big")
            batch.clear()

    for zpath in sorted((bronze_dir / "gdelt").glob("*.export.CSV.zip")):
        meta = lookup.get(zpath.name)
        df = gdelt.parse_zip_cached(zpath, cache_dir)
        if df.empty:
            continue
        df = df.copy()
        df["file_name"] = zpath.name
        df["file_sha256"] = (meta or {}).get("sha256") or manifest.sha256_file(zpath)
        df["fetched_utc"] = _fetched(meta, zpath)
        batch.append(df)
        if sum(len(b) for b in batch) > 200_000:
            flush()
    flush()

    return {
        "newsagent": con.execute("SELECT count(*) FROM bronze.newsagent_raw").fetchone()[0],
        "gdelt": con.execute("SELECT count(*) FROM bronze.gdelt_events_raw").fetchone()[0],
    }

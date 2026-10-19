"""Source 2: GDELT 2.0 Events (free, open, no key).

GDELT publishes one export file every 15 minutes (96/day):
    http://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.export.CSV.zip
Taking all of them for 2 years is ~70,000 files, so we sample `per_day` evenly spaced slots per
day (default 2). This is a *sample* of GDELT, and the project must say so. Raise GDELT_PER_DAY
for more volume. Original zips are kept untouched (bronze); the Pakistan-only rows
(ActionGeo_CountryCode == PK) are extracted from them when the warehouse is built.
"""
from __future__ import annotations

import csv
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from civicpulse import config
from civicpulse.ingest import manifest

GDELT_COLS = [
    "GLOBALEVENTID", "SQLDATE", "MonthYear", "Year", "FractionDate",
    *[f"Actor1{s}" for s in ("Code", "Name", "CountryCode", "KnownGroupCode", "EthnicCode",
                             "Religion1Code", "Religion2Code", "Type1Code", "Type2Code", "Type3Code")],
    *[f"Actor2{s}" for s in ("Code", "Name", "CountryCode", "KnownGroupCode", "EthnicCode",
                             "Religion1Code", "Religion2Code", "Type1Code", "Type2Code", "Type3Code")],
    "IsRootEvent", "EventCode", "EventBaseCode", "EventRootCode", "QuadClass", "GoldsteinScale",
    "NumMentions", "NumSources", "NumArticles", "AvgTone",
    *[f"Actor1Geo_{s}" for s in ("Type", "FullName", "CountryCode", "ADM1Code", "ADM2Code", "Lat", "Long", "FeatureID")],
    *[f"Actor2Geo_{s}" for s in ("Type", "FullName", "CountryCode", "ADM1Code", "ADM2Code", "Lat", "Long", "FeatureID")],
    *[f"ActionGeo_{s}" for s in ("Type", "FullName", "CountryCode", "ADM1Code", "ADM2Code", "Lat", "Long", "FeatureID")],
    "DATEADDED", "SOURCEURL",
]
assert len(GDELT_COLS) == 61


def slot_stamps(start: date, end: date, per_day: int) -> list[str]:
    per_day = max(1, min(96, per_day))
    step = 96 // per_day
    stamps = []
    d = start
    while d <= end:
        for i in range(per_day):
            minutes = i * step * 15
            t = datetime(d.year, d.month, d.day) + timedelta(minutes=minutes)
            stamps.append(t.strftime("%Y%m%d%H%M%S"))
        d += timedelta(days=1)
    return stamps


def _download(stamp: str, out_dir: Path, retries: int = 3) -> tuple[str, Path | None, str]:
    name = f"{stamp}.export.CSV.zip"
    dest = out_dir / name
    if dest.exists():
        return stamp, dest, "cached"
    url = config.GDELT_BASE_URL + name
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 404:
                return stamp, None, "missing"  # GDELT has occasional gaps
            r.raise_for_status()
            part = dest.with_suffix(".part")
            part.write_bytes(r.content)
            zipfile.ZipFile(part).testzip()  # reject truncated downloads
            part.rename(dest)
            manifest.make_read_only(dest)
            return stamp, dest, "downloaded"
        except (requests.RequestException, zipfile.BadZipFile):
            time.sleep(2 * (attempt + 1))
    return stamp, None, "failed"


def fetch_range(
    start: date, end: date, per_day: int,
    bronze_dir: Path = config.BRONZE_DIR, manifest_path: Path = config.MANIFEST_PATH,
    workers: int = 4, max_files: int | None = None,
) -> dict[str, int]:
    out_dir = bronze_dir / "gdelt"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamps = slot_stamps(start, end, per_day)
    if max_files:
        stamps = stamps[:max_files]
    known = {r["file_path"] for r in manifest.read_manifest(manifest_path)}
    counts = {"downloaded": 0, "cached": 0, "missing": 0, "failed": 0}
    pending_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_download, s, out_dir) for s in stamps]
        for i, fut in enumerate(as_completed(futures), 1):
            stamp, path, status = fut.result()
            counts[status] += 1
            if path is not None and manifest.rel_path(path) not in known:
                pending_rows.append({
                    "file_path": manifest.rel_path(path), "source": "gdelt",
                    "source_url": config.GDELT_BASE_URL + path.name,
                    "search_query": "gdeltv2 events export slot; PK filter (ActionGeo_CountryCode=PK) applied at load",
                    "fetch_timestamp_utc": manifest.utc_now_iso(),
                    "sha256": manifest.sha256_file(path), "bytes": path.stat().st_size,
                    "license_terms": config.GDELT_LICENSE,
                })
            if len(pending_rows) >= 100:  # flush so an interrupted run keeps its manifest
                manifest.append_manifest(pending_rows, manifest_path)
                pending_rows = []
            if i % 200 == 0:
                print(f"  {i}/{len(stamps)} files  {counts}")
    manifest.append_manifest(pending_rows, manifest_path)
    return counts



def fetch_recent(
    hours: int = 4,
    bronze_dir: Path = config.BRONZE_DIR,
    manifest_path: Path = config.MANIFEST_PATH,
    workers: int = 4,
) -> dict[str, int]:
    """Fetch recent completed 15-minute GDELT slots for hourly incremental runs."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    current_slot = now.replace(minute=(now.minute // 15) * 15)
    end = current_slot - timedelta(minutes=15)  # avoid an in-progress/current slot
    count = max(1, int(hours * 4))
    stamps = [(end - timedelta(minutes=15 * i)).strftime("%Y%m%d%H%M%S")
              for i in reversed(range(count))]
    out_dir = bronze_dir / "gdelt"
    out_dir.mkdir(parents=True, exist_ok=True)
    known = {r["file_path"] for r in manifest.read_manifest(manifest_path)}
    counts = {"downloaded": 0, "cached": 0, "missing": 0, "failed": 0}
    pending = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_download, stamp, out_dir) for stamp in stamps]
        for fut in as_completed(futures):
            stamp, path, status = fut.result()
            counts[status] += 1
            if path is not None and manifest.rel_path(path) not in known:
                pending.append({
                    "file_path": manifest.rel_path(path), "source": "gdelt",
                    "source_url": config.GDELT_BASE_URL + path.name,
                    "search_query": "recent GDELT 15-minute export; PK filter applied at load",
                    "fetch_timestamp_utc": manifest.utc_now_iso(),
                    "sha256": manifest.sha256_file(path), "bytes": path.stat().st_size,
                    "license_terms": config.GDELT_LICENSE,
                })
    manifest.append_manifest(pending, manifest_path)
    return counts


def parse_zip(zip_path: Path) -> pd.DataFrame:
    """Read one GDELT export zip and keep only Pakistan action-location rows (all columns, as text)."""
    with zipfile.ZipFile(zip_path) as zf:
        inner = zf.namelist()[0]
        with zf.open(inner) as f:
            df = pd.read_csv(
                f, sep="\t", header=None, names=GDELT_COLS, dtype=str, keep_default_na=False,
                quoting=csv.QUOTE_NONE, encoding="utf-8", encoding_errors="replace",
                on_bad_lines="skip",
            )
    return df[df["ActionGeo_CountryCode"] == "PK"].reset_index(drop=True)


def parse_zip_cached(zip_path: Path, cache_dir: Path) -> pd.DataFrame:
    """Parsing 1,000+ zips on every rebuild is slow, so the PK slice is cached as parquet."""
    cache = cache_dir / "gdelt_pk" / (zip_path.name.split(".")[0] + ".parquet")
    if cache.exists():
        return pd.read_parquet(cache)
    df = parse_zip(zip_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    df.to_parquet(tmp, index=False)
    tmp.rename(cache)
    return df


def default_window() -> tuple[date, date]:
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    return end - timedelta(days=config.GDELT_DAYS_BACK), end

"""manifest.csv: every raw file we fetch is logged here (URL, query, UTC time, SHA-256, license)."""
from __future__ import annotations

import csv
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from civicpulse import config

FIELDS = [
    "file_path", "source", "source_url", "search_query",
    "fetch_timestamp_utc", "sha256", "bytes", "license_terms",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def rel_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(config.ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def make_read_only(path: Path) -> None:
    """Bronze files are immutable once written."""
    os.chmod(path, 0o444)


def read_manifest(manifest_path: Path = config.MANIFEST_PATH) -> list[dict]:
    if not manifest_path.exists():
        return []
    with open(manifest_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_manifest(rows: list[dict], manifest_path: Path = config.MANIFEST_PATH) -> int:
    """Append rows, skipping any (file_path, sha256) pair already logged. Returns rows added."""
    if not rows:
        return 0
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    seen = {(r["file_path"], r["sha256"]) for r in read_manifest(manifest_path)}
    new = [r for r in rows if (r["file_path"], r["sha256"]) not in seen]
    if not new:
        return 0
    is_new_file = not manifest_path.exists()
    with open(manifest_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new_file:
            w.writeheader()
        for r in new:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return len(new)


def manifest_lookup(manifest_path: Path = config.MANIFEST_PATH) -> dict[str, dict]:
    """file name -> manifest row (file names are unique: timestamped)."""
    return {Path(r["file_path"]).name: r for r in read_manifest(manifest_path)}

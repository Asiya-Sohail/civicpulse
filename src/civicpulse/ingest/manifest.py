"""manifest.csv: every raw file we fetch is logged here (URL, query, UTC time, SHA-256, license)."""
from __future__ import annotations

import csv
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from civicpulse import config

RAW_SOURCE_DIRS = {"freenewsapi", "gdelt", "open_meteo"}
FIELDS = [
    "file_path", "source", "source_url", "search_query",
    "fetch_timestamp_utc", "sha256", "bytes", "license_terms",
]


class ManifestIntegrityError(RuntimeError):
    """Raised when on-disk Bronze files do not match their manifest records."""


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
    """Absolute file path -> manifest row."""
    result = {}
    for row in read_manifest(manifest_path):
        path = Path(row["file_path"])
        if not path.is_absolute():
            path = config.ROOT / path
        result[str(path.resolve())] = row
    return result


def validate_bronze_inventory(
    bronze_dir: Path = config.BRONZE_DIR,
    manifest_path: Path = config.MANIFEST_PATH,
) -> dict[str, dict]:
    """Require a complete, valid manifest and matching SHA-256 for every Bronze file."""
    bronze_root = bronze_dir.resolve()
    rows = read_manifest(manifest_path)
    manifest_by_path: dict[str, dict] = {}
    for row in rows:
        raw_path = Path(row.get("file_path", ""))
        file_path = raw_path if raw_path.is_absolute() else config.ROOT / raw_path
        resolved = file_path.resolve()
        try:
            relative = resolved.relative_to(bronze_root)
        except ValueError:
            raise ManifestIntegrityError(f"Manifest path is outside Bronze: {row.get('file_path')}")
        source_dir = relative.parts[0] if relative.parts else ""
        if source_dir not in RAW_SOURCE_DIRS:
            raise ManifestIntegrityError(f"Unknown Bronze source directory: {row.get('file_path')}")
        missing_fields = [
            field for field in ("source", "source_url", "search_query", "fetch_timestamp_utc", "sha256", "license_terms")
            if not row.get(field)
        ]
        if missing_fields:
            raise ManifestIntegrityError(
                f"Manifest entry {row.get('file_path')} is missing required fields: {', '.join(missing_fields)}"
            )
        if row["source"] != source_dir:
            raise ManifestIntegrityError(
                f"Manifest source mismatch for {row['file_path']}: expected {source_dir}, got {row['source']}"
            )
        if str(resolved) in manifest_by_path:
            raise ManifestIntegrityError(f"Duplicate manifest path: {row['file_path']}")
        manifest_by_path[str(resolved)] = row

    disk_files = {
        str(path.resolve()): path
        for path in bronze_root.rglob("*")
        if path.is_file()
    } if bronze_root.exists() else {}

    unmanifested = sorted(set(disk_files) - set(manifest_by_path))
    if unmanifested:
        raise ManifestIntegrityError(
            f"Unmanifested Bronze file(s): {', '.join(unmanifested[:5])}"
        )

    missing = sorted(set(manifest_by_path) - set(disk_files))
    if missing:
        raise ManifestIntegrityError(
            f"Manifest file(s) missing from Bronze: {', '.join(missing[:5])}"
        )

    for key, path in disk_files.items():
        entry = manifest_by_path[key]
        actual_sha = sha256_file(path)
        expected_sha = entry["sha256"].strip().lower()
        if actual_sha != expected_sha:
            raise ManifestIntegrityError(
                f"SHA-256 mismatch for {entry['file_path']}: expected {expected_sha}, got {actual_sha}"
            )
        expected_bytes = entry.get("bytes")
        if expected_bytes and int(expected_bytes) != path.stat().st_size:
            raise ManifestIntegrityError(
                f"Size mismatch for {entry['file_path']}: expected {expected_bytes}, got {path.stat().st_size}"
            )
    return manifest_by_path

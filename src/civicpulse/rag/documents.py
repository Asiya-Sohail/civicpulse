"""Shared document shape and chunking for web pages, project notes, and Gold rows."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class RAGDocument:
    """One evidence chunk with the same citation/provenance fields for every source."""

    document_id: str
    title: str
    source_url: str
    publisher: str
    published_at: str | None
    accessed_at: str
    location: str
    text: str
    chunk_id: str
    evidence_type: str = "document"
    row_id: str = ""
    table_name: str = ""
    city: str = ""
    issue_type: str = ""
    hour_utc: str = ""
    n_reports: int | None = None
    license_terms: str = ""
    manifest_ids: str = ""
    bronze_file_paths: str = ""

    def as_record(self) -> dict:
        """Return a regular dictionary suitable for JSON/context output."""
        return asdict(self)

    def as_vector_metadata(self) -> dict[str, str]:
        """Chroma metadata allows scalar values; serialize nullable values as strings."""
        return {
            key: "" if value is None else str(value)
            for key, value in self.as_record().items()
            if key != "text"
        }


def stable_id(*parts: str) -> str:
    """Make deterministic IDs so rebuilding the same source does not duplicate chunks."""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def split_into_chunks(text: str, chunk_size: int = 900, overlap: int = 120) -> list[str]:
    """Split text into overlapping character chunks while keeping paragraph boundaries."""
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("Require chunk_size > overlap >= 0.")
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + chunk_size, len(normalized))
        if end < len(normalized):
            boundary = normalized.rfind(" ", start, end)
            if boundary > start:
                end = boundary
        chunks.append(normalized[start:end].strip())
        if end >= len(normalized):
            break
        next_start = max(start + 1, end - overlap)
        # Keep the overlap near the requested size but start at a word boundary.
        if next_start < len(normalized) and normalized[next_start] != " ":
            previous_space = normalized.rfind(" ", start, next_start)
            if previous_space >= start:
                next_start = previous_space + 1
            else:
                next_space = normalized.find(" ", next_start)
                if next_space >= 0:
                    next_start = next_space + 1
        start = next_start
    return chunks


def utc_today() -> str:
    """Return today's UTC date for the shared accessed_at metadata field."""
    return datetime.now(timezone.utc).date().isoformat()

"""Fetch selected official pages and relevant same-site links after checking robots.txt."""
from __future__ import annotations

import hashlib
import re
import shutil
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from civicpulse import config
from civicpulse.ingest import manifest
from civicpulse.rag.documents import RAGDocument, split_into_chunks, stable_id, utc_today

USER_AGENT = "CivicPulseRAG/0.1 (+https://github.com/amnaaltaf98-gif/civicpulse)"
MAX_PAGE_BYTES = 2_000_000
UNVERIFIED_TERMS = "Reuse terms/licence must be checked manually before indexing or sharing."


@dataclass(frozen=True)
class Source:
    """A manually selected official-site entry point; the fetcher does not crawl its links."""

    name: str
    publisher: str
    url: str
    location: str


# One page per source; local PDFs replace the Citizen Portal website and historical NDMA fetches.
SOURCES = (
    Source("PMD public information", "Pakistan Meteorological Department", "https://weather.gov.pk/", "Pakistan"),
    Source("KWSC public information", "Karachi Water and Sewerage Corporation", "https://www.kwsc.gos.pk/", "Karachi"),
    Source("K-Electric public information", "K-Electric", "https://ke.com.pk/", "Karachi"),
)

LOCAL_PDF_PUBLISHERS = {
    "NDMA.pdf": "Provincial Disaster Management Authority Sindh / National Disaster Management Authority",
    "PCP.pdf": "Prime Minister's Performance Delivery Unit (Pakistan Citizen's Portal)",
}


def _robots_allows(url: str, session: requests.Session) -> bool:
    """Fail closed when robots.txt cannot be checked; robots permission is not a reuse licence."""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    response = session.get(robots_url, timeout=(10, 30), headers={"User-Agent": USER_AGENT})
    if response.status_code == 404:
        return True
    response.raise_for_status()
    parser = RobotFileParser(robots_url)
    parser.parse(response.text.splitlines())
    return parser.can_fetch(USER_AGENT, url)


def _visible_page_text(content: bytes, content_type: str) -> tuple[str, str]:
    """Extract readable text from an HTML page or a text-based PDF."""
    if "pdf" in content_type.lower():
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError('PDF parsing requires the RAG extra: python -m pip install -e ".[rag]"') from exc
        reader = PdfReader(BytesIO(content))
        title = str(reader.metadata.title or "Official public PDF") if reader.metadata else "Official public PDF"
        return title, "\n".join(page.extract_text() or "" for page in reader.pages)

    soup = BeautifulSoup(content, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else "Official public information"
    for element in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        element.decompose()
    main = soup.find("main") or soup.body or soup
    text = main.get_text("\n", strip=True)
    return title, text


def _read_local_pdfs(out_dir: Path, manifest_path: Path) -> None:
    """Archive user-provided PDFs as immutable Bronze files with manifest provenance."""
    pdf_dir = config.ROOT / "data" / "pdf"
    for source_path in sorted(pdf_dir.glob("*.pdf")):
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        raw_path = out_dir / f"{digest}.pdf"
        if not raw_path.exists():
            shutil.copyfile(source_path, raw_path)
            manifest.make_read_only(raw_path)
        publisher = LOCAL_PDF_PUBLISHERS.get(source_path.name, "User-provided PDF; publisher needs review")
        if source_path.name == "NDMA.pdf":
            terms = "User-provided copy; project documentation notes NDMA reuse restrictions. Verify permission before sharing."
        else:
            terms = "User-provided local PDF; original publisher reuse terms need manual verification."
        manifest.append_manifest([{
            "file_path": manifest.rel_path(raw_path),
            "source": "rag_sources",
            "source_url": f"localfile://data/pdf/{source_path.name}",
            "search_query": f"User-provided local PDF; publisher={publisher}",
            "fetch_timestamp_utc": manifest.utc_now_iso(),
            "sha256": digest,
            "bytes": raw_path.stat().st_size,
            "license_terms": terms,
        }], manifest_path)


def _fetch_one(session: requests.Session, url: str, source: Source, out_dir: Path,
               manifest_path: Path) -> tuple[list[RAGDocument], bytes | None, list[str]]:
    """Check access rules, fetch one bounded page, archive it, and normalize its chunks."""
    if not _robots_allows(url, session):
        return [], None, [f"Skipped {url}: robots.txt disallows this page."]
    response = session.get(url, timeout=(10, 45), headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf"})
    response.raise_for_status()
    if urlparse(response.url).netloc.lower() != urlparse(url).netloc.lower():
        return [], None, [f"Skipped {url}: it redirected to a different host ({response.url})."]
    content_type = response.headers.get("Content-Type", "").lower()
    if not any(kind in content_type for kind in ("html", "pdf")):
        return [], None, [f"Skipped {url}: response was not HTML or PDF."]
    raw = response.content
    if len(raw) > MAX_PAGE_BYTES:
        return [], None, [f"Skipped {url}: response exceeded the 2 MB limit."]

    digest = hashlib.sha256(raw).hexdigest()
    suffix = ".pdf" if "pdf" in content_type else ".html"
    raw_path = out_dir / f"{digest}{suffix}"
    if not raw_path.exists():
        raw_path.write_bytes(raw)
        manifest.make_read_only(raw_path)
    manifest.append_manifest([{
        "file_path": manifest.rel_path(raw_path),
        "source": "rag_sources",
        "source_url": response.url,
        "search_query": "Manually selected official page or relevant same-site notice link",
        "fetch_timestamp_utc": manifest.utc_now_iso(),
        "sha256": digest,
        "bytes": len(raw),
        "license_terms": UNVERIFIED_TERMS,
    }], manifest_path)
    manifest_id = stable_id(manifest.rel_path(raw_path), digest)

    title, text = _visible_page_text(raw, content_type)
    records = []
    for number, chunk in enumerate(split_into_chunks(text), start=1):
        chunk_key = f"{source.name}:{digest}:{number}"
        records.append(RAGDocument(
            document_id=stable_id(response.url, digest),
            title=title or source.name,
            source_url=response.url,
            publisher=source.publisher,
            published_at=None,
            accessed_at=utc_today(),
            location=source.location,
            text=chunk,
            chunk_id=f"chunk_{number:04d}_{stable_id(chunk_key)[:10]}",
            evidence_type="document",
            license_terms=UNVERIFIED_TERMS,
            manifest_ids=manifest_id,
            bronze_file_paths=manifest.rel_path(raw_path),
        ))
    notices = [f"Fetched {url}, but no readable text was extracted."] if not records else []
    return records, raw if "html" in content_type else None, notices


def fetch_source_pages(
    bronze_dir: Path = config.BRONZE_DIR,
    manifest_path: Path = config.MANIFEST_PATH,
) -> tuple[list[RAGDocument], list[str]]:
    """Archive local PDFs, then fetch only one configured page from each necessary website."""
    out_dir = bronze_dir / "rag_sources"
    out_dir.mkdir(parents=True, exist_ok=True)
    docs: list[RAGDocument] = []
    warnings: list[str] = []
    _read_local_pdfs(out_dir, manifest_path)

    with requests.Session() as session:
        for source in SOURCES:
            try:
                page_docs, _, notes = _fetch_one(session, source.url, source, out_dir, manifest_path)
                docs.extend(page_docs)
                warnings.extend(notes)
            except requests.RequestException as exc:
                warnings.append(f"Could not fetch {source.url}: {type(exc).__name__}: {exc}")

    return docs, warnings


def project_reference_documents(root: Path = config.ROOT) -> list[RAGDocument]:
    """Use existing project facts as a local model/data-limitations reference, not invented claims."""
    docs: list[RAGDocument] = []
    for filename in ("MODEL_CARD.md", "MODEL_EVALUATION.md", "DATA_SOURCES.md", "README.md"):
        path = root / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        for number, chunk in enumerate(split_into_chunks(text), start=1):
            docs.append(RAGDocument(
                document_id=stable_id("repository", filename, digest),
                title=f"CivicPulse project reference: {filename}",
                source_url=f"repository://{filename}",
                publisher="CivicPulse project documentation",
                published_at=None,
                accessed_at=utc_today(),
                location="Pakistan",
                text=chunk,
                chunk_id=f"chunk_{number:04d}_{stable_id(filename, digest, str(number))[:10]}",
                evidence_type="project_document",
                license_terms="Project documentation; cite repository file.",
            ))
    return docs


def archived_source_documents(
    bronze_dir: Path = config.BRONZE_DIR,
    manifest_path: Path = config.MANIFEST_PATH,
) -> list[RAGDocument]:
    """Rebuild from local PDFs and the latest allowlisted web snapshots only."""
    permitted_urls = {source.url for source in SOURCES}
    local_urls = {f"localfile://data/pdf/{name}" for name in LOCAL_PDF_PUBLISHERS}
    latest: dict[str, dict] = {}
    for row in manifest.read_manifest(manifest_path):
        if row.get("source") != "rag_sources":
            continue
        url = row.get("source_url", "")
        # Ignore snapshots fetched by the previous, broader version of the crawler.
        if url not in permitted_urls | local_urls:
            continue
        previous = latest.get(url)
        if previous is None or row.get("fetch_timestamp_utc", "") > previous.get("fetch_timestamp_utc", ""):
            latest[url] = row

    docs: list[RAGDocument] = []
    for url, row in latest.items():
        raw_path = Path(row["file_path"])
        if not raw_path.is_absolute():
            raw_path = config.ROOT / raw_path
        if not raw_path.exists() or manifest.sha256_file(raw_path) != row.get("sha256", ""):
            raise manifest.ManifestIntegrityError(f"RAG source snapshot failed manifest verification: {raw_path}")
        content_type = "application/pdf" if raw_path.suffix.lower() == ".pdf" else "text/html"
        host = urlparse(url).netloc.lower()
        source = next((item for item in SOURCES if urlparse(item.url).netloc.lower() == host), None)
        if url.startswith("localfile://"):
            filename = Path(urlparse(url).path).name
            publisher = LOCAL_PDF_PUBLISHERS.get(filename, "User-provided PDF; publisher needs review")
            location = "Sindh, Pakistan" if filename == "NDMA.pdf" else "Pakistan"
            title = "Historical Sindh rain advisory (local PDF)" if filename == "NDMA.pdf" else "Pakistan Citizen's Portal complaint guidelines"
        else:
            publisher = source.publisher if source else "Official public source"
            location = source.location if source else "Pakistan"
            title, _ = _visible_page_text(raw_path.read_bytes(), content_type)
        digest = row["sha256"]
        published_at = None
        if content_type == "application/pdf":
            from pypdf import PdfReader
            pages = [page.extract_text() or "" for page in PdfReader(BytesIO(raw_path.read_bytes())).pages]
            if url.endswith("NDMA.pdf"):
                published_at = _find_publication_date("\n".join(pages[:2]))
            for page_number, page_text in enumerate(pages, start=1):
                if url.endswith("PCP.pdf"):
                    page_text = _decode_rot3_if_needed(page_text)
                if url.endswith("NDMA.pdf") and published_at:
                    page_text = f"HISTORICAL ADVISORY ONLY — issued {published_at}; not a current warning.\n" + page_text
                for number, chunk in enumerate(split_into_chunks(page_text), start=1):
                    docs.append(_make_archived_record(
                        url, row, digest, publisher, location, f"{title} — page {page_number}",
                        chunk, f"p{page_number:03d}_c{number:02d}", published_at,
                    ))
        else:
            _, text = _visible_page_text(raw_path.read_bytes(), content_type)
            for number, chunk in enumerate(split_into_chunks(text), start=1):
                docs.append(_make_archived_record(
                    url, row, digest, publisher, location, title, chunk,
                    f"chunk_{number:04d}", published_at,
                ))
    return docs


def _make_archived_record(url: str, row: dict, digest: str, publisher: str,
                          location: str, title: str, chunk: str,
                          chunk_label: str, published_at: str | None) -> RAGDocument:
    """Create the common metadata record used for a local PDF or website chunk."""
    chunk_key = f"{url}:{digest}:{chunk_label}"
    return RAGDocument(
        document_id=stable_id(url, digest),
        title=title,
        source_url=url,
        publisher=publisher,
        published_at=published_at,
        accessed_at=row.get("fetch_timestamp_utc", "")[:10],
        location=location,
        text=chunk,
        chunk_id=f"{chunk_label}_{stable_id(chunk_key)[:10]}",
        evidence_type="document",
        license_terms=row.get("license_terms", UNVERIFIED_TERMS),
        manifest_ids=stable_id(row["file_path"], digest),
        bronze_file_paths=row["file_path"],
    )


def _find_publication_date(text: str) -> str | None:
    """Find the first written day-month-year date in the advisory."""
    match = re.search(
        r"\b(?:Dated:\s*)?(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    from datetime import datetime
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(" ".join(match.groups()), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _decode_rot3_if_needed(text: str) -> str:
    """Repair ROT-3 encoded body text present in the supplied Citizen Portal PDF."""
    tokens = re.findall(r"[A-Za-z]+", text.upper())
    hits = sum(tokens.count(word) for word in ("WKH", "DQG", "WR", "IRU", "RI", "LV"))
    if hits < 5:
        return text
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    rotated = alphabet[3:] + alphabet[:3]
    return text.translate(str.maketrans(alphabet + alphabet.lower(), rotated + rotated.lower()))

"""Source 1: keyless FreeNewsAPI search for recently crawled Pakistan articles."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from civicpulse import config
from civicpulse.ingest import manifest


def _get_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    return session


def _request_page(session: requests.Session, offset: int) -> tuple[bytes, dict]:
    params = {
        "country": config.FREENEWSAPI_COUNTRY,
        "strict_country": "true",
        "sort": "crawled",
        "from": f"now-{config.FREENEWSAPI_LOOKBACK_HOURS}h",
        "size": str(config.FREENEWSAPI_PAGE_SIZE),
        "offset": str(offset),
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": "CivicPulse/0.1.0",
        "X-Agent": (
            "name=civicpulse; version=0.1.0; framework=python-requests; "
            "contact=https://github.com/amnaaltaf98-gif/civicpulse"
        ),
    }
    url = config.FREENEWSAPI_BASE_URL + config.FREENEWSAPI_SEARCH_ENDPOINT
    try:
        response = session.get(url, params=params, headers=headers, timeout=(10, 60))
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"FreeNewsAPI request failed at offset {offset}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RuntimeError(f"FreeNewsAPI response at offset {offset} is missing a results list")
    return response.content, {
        "url": url,
        "query": urlencode(params),
        "payload": payload,
    }


def extract_items(payload) -> list[dict]:
    """Extract article objects from a FreeNewsAPI search response."""
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return [item for item in payload["results"] if isinstance(item, dict)]
    return []


def fetch_feed(
    bronze_dir: Path = config.BRONZE_DIR, manifest_path: Path = config.MANIFEST_PATH
) -> list[Path]:
    """Fetch the overlapping hourly window, page through it, and persist each raw response."""
    out_dir = bronze_dir / "freenewsapi"
    out_dir.mkdir(parents=True, exist_ok=True)
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
    paths: list[Path] = []
    manifest_rows: list[dict] = []
    offset = 0

    try:
        with _get_session() as session:
            while offset <= 9_900:
                body, request = _request_page(session, offset)
                payload = request["payload"]
                path = out_dir / f"{batch_id}_offset_{offset:05d}.json"
                path.write_bytes(body)
                manifest.make_read_only(path)
                now = datetime.now(timezone.utc)
                manifest_rows.append({
                    "file_path": manifest.rel_path(path),
                    "source": "freenewsapi",
                    "source_url": request["url"],
                    "search_query": request["query"],
                    "fetch_timestamp_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sha256": manifest.sha256_file(path),
                    "bytes": path.stat().st_size,
                    "license_terms": config.FREENEWSAPI_LICENSE,
                })
                paths.append(path)

                items = extract_items(payload)
                if len(items) < config.FREENEWSAPI_PAGE_SIZE:
                    break
                offset += len(items)
    finally:
        manifest.append_manifest(manifest_rows, manifest_path)
    if payload.get("total_is_lower_bound") and offset >= 9_900:
        print("WARNING: FreeNewsAPI hit its 10,000-result paging ceiling; this fetch may be incomplete.")
    return paths


def probe() -> None:
    """Check API connectivity and report the response/article shape without saving files."""
    with _get_session() as session:
        body, request = _request_page(session, 0)
    payload = request["payload"]
    items = extract_items(payload)
    print(f"GET {request['url']}?{request['query']} -> {len(body)} bytes")
    print(f"articles returned: {len(items)} of {payload.get('total', 'unknown')} matching")
    if items:
        print("article fields:", ", ".join(items[0].keys()))
        print(json.dumps(items[0], indent=2, ensure_ascii=False)[:1500])

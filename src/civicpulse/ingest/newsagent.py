"""Source 1: NewsAgent Data API, one pull per hour (~24 of the 100 free requests/day).

The API key travels only in a request header and is never written to disk or logs.
The raw response body is saved byte-for-byte (read-only) and logged in manifest.csv.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

from civicpulse import config
from civicpulse.ingest import manifest


class NewsAgentConfigError(RuntimeError):
    pass


def _request() -> tuple[bytes, str, str]:
    """Return (body, url_without_secrets, query_string)."""
    if not config.NEWSAGENT_API_KEY or not config.NEWSAGENT_BASE_URL:
        raise NewsAgentConfigError(
            "Set NEWSAGENT_API_KEY and NEWSAGENT_BASE_URL in .env (copy from .env.example)."
        )
    params = {"country": config.NEWSAGENT_COUNTRY}
    if config.NEWSAGENT_LIMIT:
        params["limit"] = config.NEWSAGENT_LIMIT
    value = config.NEWSAGENT_API_KEY
    if config.NEWSAGENT_AUTH_SCHEME:
        value = f"{config.NEWSAGENT_AUTH_SCHEME} {value}"
    url = config.NEWSAGENT_BASE_URL + config.NEWSAGENT_ENDPOINT
    resp = requests.get(url, params=params, headers={config.NEWSAGENT_AUTH_HEADER: value}, timeout=30)
    if resp.status_code != 200:
        # never echo headers; body may help diagnose (quota, bad key) and contains no secrets
        raise RuntimeError(f"NewsAgent HTTP {resp.status_code}: {resp.text[:200]}")
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return resp.content, url, query


def fetch_feed(
    bronze_dir: Path = config.BRONZE_DIR, manifest_path: Path = config.MANIFEST_PATH
) -> Path:
    body, url, query = _request()
    json.loads(body)  # fail loudly if it is not JSON; we still store exactly what we received
    now = datetime.now(timezone.utc)
    out_dir = bronze_dir / "newsagent"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{now.strftime('%Y%m%dT%H%M%SZ')}_feed.json"
    path.write_bytes(body)
    manifest.make_read_only(path)
    manifest.append_manifest(
        [{
            "file_path": manifest.rel_path(path), "source": "newsagent",
            "source_url": url, "search_query": query,
            "fetch_timestamp_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sha256": manifest.sha256_file(path), "bytes": path.stat().st_size,
            "license_terms": config.NEWSAGENT_LICENSE,
        }],
        manifest_path,
    )
    return path


def extract_items(payload) -> list[dict]:
    """Find the list of articles in whatever envelope the API uses."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("articles", "items", "data", "results", "news", "feed"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
    return []


def probe() -> None:
    """Print the response shape so field mapping in config.NEWSAGENT_FIELDS can be checked."""
    body, url, query = _request()
    payload = json.loads(body)
    items = extract_items(payload)
    print(f"GET {url}?{query}  ->  {len(body)} bytes")
    if isinstance(payload, dict):
        print("top-level keys:", list(payload.keys()))
    print(f"articles found: {len(items)}")
    if items:
        print("first article keys:", list(items[0].keys()))
        print(json.dumps(items[0], indent=2, ensure_ascii=False)[:1500])
        text = " ".join(str(i.get(k, "")) for i in items for k in ("title", "headline")).lower()
        hits = sum(w in text for w in ("pakistan", "karachi", "lahore", "islamabad"))
        print(f"Pakistan keywords present in titles: {hits}/4  (low = coverage may be thin)")

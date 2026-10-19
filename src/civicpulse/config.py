"""Central configuration: paths, source settings, and the Pakistan-specific entity rules."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
BRONZE_DIR = DATA_DIR / "bronze"
CACHE_DIR = DATA_DIR / "cache"
MANIFEST_PATH = DATA_DIR / "manifest.csv"
DB_PATH = DATA_DIR / "warehouse" / "civicpulse.duckdb"

# ---- Source 1: NewsAgent Data API (live) -------------------------------------------------
NEWSAGENT_API_KEY = os.getenv("NEWSAGENT_API_KEY", "")
NEWSAGENT_BASE_URL = os.getenv("NEWSAGENT_BASE_URL", "https://api.newsagentdata.com").strip().rstrip("/").removesuffix("/v1")
NEWSAGENT_ENDPOINT = os.getenv("NEWSAGENT_ENDPOINT", "/v1/feed")
NEWSAGENT_AUTH_HEADER = os.getenv("NEWSAGENT_AUTH_HEADER", "X-API-Key")
NEWSAGENT_AUTH_SCHEME = os.getenv("NEWSAGENT_AUTH_SCHEME", "")
NEWSAGENT_COUNTRY = os.getenv("NEWSAGENT_COUNTRY", "pk")
NEWSAGENT_PAGE_SIZE = os.getenv("NEWSAGENT_PAGE_SIZE", "100")  # API max is 200
NEWSAGENT_LICENSE = (
    "NewsAgent Data free tier (100 req/day). Provider terms apply - confirm redistribution "
    "rights at newsagentdata.com before publishing raw data."
)
# Verified against https://api.newsagentdata.com/openapi.json: articles[] has id, title, source, link,
# fetched_at, urgency_score, country_tags, topic_tags, event_type. Extra names are kept as fallbacks.
NEWSAGENT_FIELDS = {
    "title": ["title", "headline"],
    "url": ["link", "url", "source_url"],
    # NewsAgent only exposes fetched_at (when *they* fetched it), used as the publish time
    "published": ["published_at", "published", "timestamp", "pub_date", "date", "created_at", "fetched_at"],
    "urgency": ["urgency", "urgency_score", "urgency_level"],
    "topic": ["topic", "topics", "topic_tags", "category", "event_type"],
    "publisher": ["source", "publisher", "source_name", "outlet"],
    "country": ["country", "countries", "country_tags", "country_tag"],
    "id": ["id", "article_id", "uuid"],
}

# ---- Source 2: GDELT 2.0 Events (historical) ---------------------------------------------
GDELT_BASE_URL = "http://data.gdeltproject.org/gdeltv2/"
GDELT_DAYS_BACK = int(os.getenv("GDELT_DAYS_BACK", "740"))
GDELT_PER_DAY = int(os.getenv("GDELT_PER_DAY", "2"))
GDELT_LICENSE = (
    "GDELT Project terms: unlimited, unrestricted use (academic, commercial, governmental) "
    "without fee; any use or redistribution must cite the GDELT Project and link "
    "https://www.gdeltproject.org/"
)

# ---- Data quality thresholds -------------------------------------------------------------
STALE_AFTER_HOURS = float(os.getenv("STALE_AFTER_HOURS", "3"))
PK_BBOX = (23.5, 37.2, 60.8, 77.9)  # lat_min, lat_max, lon_min, lon_max (rough Pakistan box)
MIN_PUBLISHED = "2015-02-01"  # GDELT 2.0 starts Feb 2015
MIN_DOCS, MIN_ROWS = 5_000, 50_000  # hackathon volume requirement (docs OR rows)

# ---- Entity rules ------------------------------------------------------------------------
KNOWN_CITIES = [
    "Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad", "Multan", "Peshawar",
    "Quetta", "Hyderabad", "Gujranwala", "Sialkot", "Sukkur", "Larkana", "Bahawalpur",
    "Sargodha", "Abbottabad", "Mardan", "Gwadar",
]
# lower-cased variant -> canonical
CITY_ALIASES = {
    "khi": "Karachi", "karchi": "Karachi", "karachi city": "Karachi", "krachi": "Karachi",
    "lhr": "Lahore", "lahor": "Lahore",
    "isb": "Islamabad", "ict": "Islamabad", "islamabad capital territory": "Islamabad",
    "rwp": "Rawalpindi", "pindi": "Rawalpindi",
    "pesh": "Peshawar",
}
MAJOR_REGIONS = {"Karachi", "Lahore", "Islamabad"}

_CITY_RE = re.compile(r"\b(" + "|".join(KNOWN_CITIES) + r")\b", re.I)
_CITY_ABBR_RE = re.compile(r"\b(KHI|LHR|ISB)\b")  # case-sensitive on purpose
_ABBR = {"KHI": "Karachi", "LHR": "Lahore", "ISB": "Islamabad"}

ISSUE_PATTERNS = {
    "Electricity": [
        r"load[\s-]?shedding", r"power (outage|outages|cut|cuts|failure|breakdown|supply|shutdown)",
        r"black[\s-]?out", r"electricity", r"\bgrid\b", r"tripp(ed|ing)", r"transformer",
        r"\bk[\s-]?electric\b", r"\bwapda\b", r"\b(lesco|iesco|fesco|gepco|mepco|pesco|hesco|sepco|qesco|tesco)\b",
    ],
    "Water": [
        r"water (shortage|supply|crisis|scarcity|cut|outage|tanker|tankers|board|line|lines|pipeline)",
        r"\b(kwsb|kwsc|wasa)\b", r"tanker mafia", r"drinking water",
    ],
    "Mobility": [
        r"traffic", r"road (closure|closed|blocked|block|blockade|collapse)", r"congestion",
        r"gridlock", r"\bjam\b", r"diversion", r"flyover", r"underpass", r"metro ?bus",
        r"\bcommuters?\b",
    ],
    "Municipal": [
        r"garbage", r"sewerage", r"sanitation", r"\bdrain(s|age)?\b", r"waste (collection|management)",
        r"\bsui gas\b", r"gas (outage|shortage|load[\s-]?shedding|suspension)", r"\b(ssgc|sngpl)\b",
        r"street ?lights?", r"urban flood", r"solid waste",
    ],
}
_ISSUE_RE = {k: [re.compile(p, re.I) for p in v] for k, v in ISSUE_PATTERNS.items()}

SERVICE_PATTERNS = {
    "K-Electric": r"\bk[\s-]?electric\b", "WAPDA": r"\bwapda\b", "LESCO": r"\blesco\b",
    "IESCO": r"\biesco\b", "FESCO": r"\bfesco\b", "GEPCO": r"\bgepco\b", "MEPCO": r"\bmepco\b",
    "PESCO": r"\bpesco\b", "HESCO": r"\bhesco\b", "SEPCO": r"\bsepco\b", "QESCO": r"\bqesco\b",
    "KWSB": r"\b(kwsb|kwsc)\b", "WASA": r"\bwasa\b", "SSGC": r"\bssgc\b", "SNGPL": r"\bsngpl\b",
}
_SERVICE_RE = {k: re.compile(p, re.I) for k, p in SERVICE_PATTERNS.items()}


def url_to_text(url: str | None) -> str:
    """Turn a news URL slug into plain words ('karachi-load-shedding' -> 'karachi load shedding')."""
    if not isinstance(url, str) or not url:
        return ""
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        return ""
    return re.sub(r"[^A-Za-z0-9]+", " ", path).strip()


def classify_issue(text: str) -> str:
    best, best_n = "Other", 0
    for issue, pats in _ISSUE_RE.items():  # dict order = tie-break priority
        n = sum(1 for p in pats if p.search(text))
        if n > best_n:
            best, best_n = issue, n
    return best


def detect_service(text: str) -> str | None:
    for name, pat in _SERVICE_RE.items():
        if pat.search(text):
            return name
    return None


def detect_city(text: str) -> str | None:
    m = _CITY_ABBR_RE.search(text)
    if m:
        return _ABBR[m.group(1)]
    m = _CITY_RE.search(text)
    return m.group(1).title() if m else None


def region_group(city: str | None) -> str:
    if not city:
        return "Unknown"
    return city if city in MAJOR_REGIONS else "Other Districts"

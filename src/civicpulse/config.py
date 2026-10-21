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
OPEN_METEO_BRONZE_DIR = BRONZE_DIR / "open_meteo"
CACHE_DIR = DATA_DIR / "cache"
MANIFEST_PATH = DATA_DIR / "manifest.csv"
DB_PATH = DATA_DIR / "warehouse" / "civicpulse.duckdb"
RAG_DIR = DATA_DIR / "rag"
RAG_VECTORSTORE_DIR = RAG_DIR / "chroma"
RAG_MODEL_NAME = os.getenv(
    "RAG_EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

# ---- Source 1: FreeNewsAPI (keyless live search) ----------------------------------------
FREENEWSAPI_BASE_URL = "https://freenewsapi.ai"
FREENEWSAPI_SEARCH_ENDPOINT = "/v1/search"
FREENEWSAPI_COUNTRY = "PK"
FREENEWSAPI_PAGE_SIZE = 100
FREENEWSAPI_LOOKBACK_HOURS = int(os.getenv("FREENEWSAPI_LOOKBACK_HOURS", "4"))
FREENEWSAPI_LICENSE = (
    "FreeNewsAPI.ai Terms of Use permit API data use, storage, and processing; link to the "
    "original publisher URL. Article text remains publisher-owned; FreeNewsAPI grants no "
    "licence to that text. https://freenewsapi.ai/legal/terms"
)

# ---- Source 2: GDELT 2.0 Events (historical) ---------------------------------------------
GDELT_BASE_URL = "http://data.gdeltproject.org/gdeltv2/"
GDELT_DAYS_BACK = max(730, int(os.getenv("GDELT_DAYS_BACK", "740")))
GDELT_PER_DAY = max(4, int(os.getenv("GDELT_PER_DAY", "4")))
GDELT_BACKFILL_MARKER = DATA_DIR / "warehouse" / ".gdelt-history-complete-v5"
GDELT_LICENSE = (
    "GDELT Project terms: unlimited, unrestricted use (academic, commercial, governmental) "
    "without fee; any use or redistribution must cite the GDELT Project and link "
    "https://www.gdeltproject.org/"
)
# ---- Source 3: Open-Meteo historical weather -------------------------------
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_LICENSE = (
    "Open-Meteo historical weather data under CC BY 4.0; "
    "credit Open-Meteo and the underlying weather data providers."
)
OPEN_METEO_HOURLY_VARIABLES = (
    "temperature_2m",
    "precipitation",
    "wind_speed_10m",
)
OPEN_METEO_TIMEZONE = "UTC"
# ---- Data quality thresholds -------------------------------------------------------------
STALE_AFTER_HOURS = float(os.getenv("STALE_AFTER_HOURS", "3"))
PK_BBOX = (23.5, 37.2, 60.8, 77.9)  # lat_min, lat_max, lon_min, lon_max (rough Pakistan box)
MIN_PUBLISHED = "2015-02-01"  # GDELT 2.0 starts Feb 2015
MIN_DOCS, MIN_ROWS = 5_000, 50_000  # hackathon volume requirement (docs OR rows)
DATASET_RAW_ROW_TARGET = 60_000

# ---- Step 4: locked forecasting evaluation ---------------------------------
FORECAST_CUTOFF_UTC = "2026-04-06T18:00:00Z"
FORECAST_LAG_HOURS = 168

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

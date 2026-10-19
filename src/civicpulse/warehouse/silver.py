"""Silver: both sources mapped to one typed schema, entities extracted, then passed through the DQ gate."""
from __future__ import annotations

import hashlib
import json

import duckdb
import numpy as np
import pandas as pd

from civicpulse import config
from civicpulse.quality.agent import DataQualityAgent

SILVER_COLS = [
    "article_id", "source", "source_record_id", "title", "url", "publisher", "publisher_domain",
    "published_utc", "crawled_utc", "fetched_utc", "city", "province", "region_group", "lat", "lon",
    "issue_type", "service", "urgency_score", "cameo_code", "avg_tone", "raw_topic", "country_tag",
    "is_imputed", "imputed_fields", "verification_status", "dq_flags",
]


def _flat(v):
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return v


def _ts(v):
    if v is None:
        return pd.NaT
    if isinstance(v, (int, float)):
        return pd.to_datetime(v, unit="ms" if v > 1e11 else "s", errors="coerce")
    t = pd.to_datetime(str(v), errors="coerce", utc=True)
    return t.tz_localize(None) if pd.notna(t) else pd.NaT


def _empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in SILVER_COLS})


def _entities(text: str, geo_city: str | None):
    city = geo_city or config.detect_city(text)
    return city, config.classify_issue(text), config.detect_service(text)


def freenewsapi_frame(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    raw = con.execute("SELECT raw_id, fetched_utc, raw_json FROM bronze.freenewsapi_raw").df()
    rows = []
    for r in raw.itertuples(index=False):
        d = json.loads(r.raw_json)
        title, url = d.get("title"), d.get("url")
        topic = _flat(d.get("categories"))
        text = f"{title or ''} {config.url_to_text(url)} {topic or ''}"
        city, issue, service = _entities(text, None)
        rows.append({
            "source": "freenewsapi", "source_record_id": str(d.get("id") or r.raw_id),
            "title": title, "url": url, "publisher": d.get("sitename") or d.get("host"),
            "published_utc": _ts(d.get("published_at")), "crawled_utc": _ts(d.get("crawled_at")),
            "fetched_utc": r.fetched_utc, "city": city, "province": None,
            "lat": np.nan, "lon": np.nan, "issue_type": issue, "service": service,
            "urgency_score": np.nan, "cameo_code": None, "avg_tone": np.nan,
            "raw_topic": topic, "country_tag": d.get("country", "PK"),
        })
    return pd.DataFrame(rows) if rows else _empty()


def gdelt_frame(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    g = con.execute(
        """SELECT GLOBALEVENTID, SOURCEURL, DATEADDED, SQLDATE, ActionGeo_FullName, ActionGeo_Lat,
                  ActionGeo_Long, EventCode, AvgTone, fetched_utc FROM bronze.gdelt_events_raw"""
    ).df()
    if g.empty:
        return _empty()
    pub = pd.to_datetime(g["DATEADDED"], format="%Y%m%d%H%M%S", errors="coerce")
    pub = pub.fillna(pd.to_datetime(g["SQLDATE"], format="%Y%m%d", errors="coerce"))
    parts = g["ActionGeo_FullName"].fillna("").str.split(",", n=2, expand=True).reindex(columns=[0, 1, 2])
    place = parts[0].fillna("").str.strip()
    # a bare "Pakistan" is the country centroid, not a city
    place = place.where(~place.str.lower().isin(["", "pakistan"]), None)
    province = parts[1].fillna("").str.strip().replace("", None)
    texts = g["SOURCEURL"].map(config.url_to_text)
    ents = [_entities(t, p) for t, p in zip(texts, place)]
    return pd.DataFrame({
        "source": "gdelt", "source_record_id": g["GLOBALEVENTID"], "title": None, "url": g["SOURCEURL"],
        "publisher": None,
        "published_utc": pub, "crawled_utc": pd.NaT, "fetched_utc": g["fetched_utc"],
        "city": [e[0] for e in ents], "province": province,
        "lat": pd.to_numeric(g["ActionGeo_Lat"], errors="coerce"),
        "lon": pd.to_numeric(g["ActionGeo_Long"], errors="coerce"),
        "issue_type": [e[1] for e in ents], "service": [e[2] for e in ents],
        "urgency_score": np.nan, "cameo_code": g["EventCode"],
        "avg_tone": pd.to_numeric(g["AvgTone"], errors="coerce"),
        "raw_topic": None, "country_tag": "PK",
    })


def build_silver(con: duckdb.DuckDBPyConnection, agent: DataQualityAgent) -> dict[str, int]:
    cand = pd.concat([freenewsapi_frame(con), gdelt_frame(con)], ignore_index=True)
    cand["is_imputed"] = False
    cand["imputed_fields"] = ""
    cand["dq_flags"] = ""
    cand["verification_status"] = "unverified"
    cand["article_id"] = None
    cand["publisher_domain"] = None
    cand["published_utc"] = pd.to_datetime(cand["published_utc"])
    cand["crawled_utc"] = pd.to_datetime(cand["crawled_utc"])
    cand["fetched_utc"] = pd.to_datetime(cand["fetched_utc"])
    agent.log("candidate_rows", "pass", len(cand), "freenewsapi+gdelt rows entering the DQ gate")

    clean, quarantined = agent.run(cand)
    clean["region_group"] = clean["city"].map(config.region_group)
    clean = clean.reindex(columns=SILVER_COLS)

    con.execute("DROP TABLE IF EXISTS silver.articles")
    con.execute("CREATE TABLE silver.articles AS SELECT * FROM clean")
    con.execute("DROP TABLE IF EXISTS silver.quarantine")
    con.execute("CREATE TABLE silver.quarantine AS SELECT * FROM quarantined")
    return {"silver": len(clean), "quarantined": len(quarantined)}

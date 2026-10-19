import numpy as np
import pandas as pd

from civicpulse import db
from civicpulse.quality.agent import DataQualityAgent, canonical_url
from civicpulse.warehouse.silver import SILVER_COLS


def cand(**over):
    base = {
        "source": "freenewsapi", "source_record_id": "1", "title": "x", "url": "https://dawn.com/a",
        "published_utc": pd.Timestamp("2026-03-01 06:00"), "crawled_utc": pd.NaT,
        "fetched_utc": pd.Timestamp("2026-03-01 07:00"),
        "city": "Karachi", "province": None, "lat": np.nan, "lon": np.nan, "issue_type": "Water",
        "service": None, "urgency_score": 5.0, "cameo_code": None, "avg_tone": np.nan,
        "raw_topic": None, "country_tag": "pk", "is_imputed": False, "imputed_fields": "",
        "dq_flags": "", "verification_status": "unverified", "article_id": None, "publisher": "Dawn",
        "publisher_domain": None,
        "bronze_file_path": "data/bronze/freenewsapi/test.json",
        "bronze_file_sha256": "a" * 64, "manifest_id": "b" * 64,
        "manifest_source_url": "https://freenewsapi.ai/v1/search",
        "manifest_search_query": "country=PK", "manifest_fetch_utc": pd.Timestamp("2026-03-01 07:00"),
        "manifest_license_terms": "test terms",
    }
    base.update(over)
    return base


def run(rows):
    con = db.connect(":memory:")
    agent = DataQualityAgent(con, "test_run")
    clean, quar = agent.run(pd.DataFrame(rows))
    agent.flush()
    log = con.execute("SELECT * FROM quality_log").df()
    return clean, quar, log


def test_fix_whitespace_city_alias_and_url():
    clean, _, log = run([cand(title="  Water   shortage ", city=" KHI ",
                              url="HTTPS://WWW.Dawn.com/a/?utm_source=x#frag")])
    r = clean.iloc[0]
    assert r.title == "Water shortage" and r.city == "Karachi"
    assert r.url == "https://www.dawn.com/a" and r.publisher_domain == "dawn.com"
    assert (log[log.check_name == "city_normalise"].action == "fix").all()


def test_missing_values_are_not_logged_as_whitespace_fixes():
    _, _, log = run([cand(title=None, publisher=None, city=None, province=None, service=None)])
    whitespace = log[log.check_name.str.startswith("trim_whitespace:")]
    assert (whitespace.rows_affected == 0).all()
    assert (whitespace.action == "pass").all()


def test_duplicates_removed_and_freenewsapi_preferred():
    clean, _, log = run([
        cand(source="gdelt", source_record_id="g1", url="http://dawn.com/a/", title=None),
        cand(url="https://www.dawn.com/a?utm_medium=z"),
    ])
    assert len(clean) == 1 and clean.iloc[0].source == "freenewsapi"
    assert int(log[log.check_name == "duplicate_links"].rows_affected.iloc[0]) == 1


def test_fill_marks_is_imputed():
    clean, _, _ = run([
        cand(url="https://a.pk/1", urgency_score=8.0),
        cand(url="https://a.pk/2", urgency_score=np.nan, published_utc=pd.NaT, publisher=None),
    ])
    r = clean[clean.url == "https://a.pk/2"].iloc[0]
    assert r.is_imputed and r.urgency_score == 8.0 and r.publisher == "a.pk"
    assert set(r.imputed_fields.split(";")) == {"published_utc", "urgency_score", "publisher"}
    assert not clean[clean.url == "https://a.pk/1"].iloc[0].is_imputed


def test_quarantine_bad_rows_and_keep_good():
    clean, quar, log = run([
        cand(url="https://a.pk/ok"),
        cand(url=None),                                   # no url
        cand(url="https://a.pk/future", published_utc=pd.Timestamp("2099-01-01")),
        cand(url="https://a.pk/urg", urgency_score=11.0),  # out of range
    ])
    assert list(clean.url) == ["https://a.pk/ok"]
    assert len(quar) == 3
    reasons = ";".join(quar.quarantine_reason)
    assert "url_present_http" in reasons and "published_in_range" in reasons and "urgency_0_to_10" in reasons
    assert (log.action == "quarantine").sum() >= 3


def test_schema_quarantines_wrong_scalar_types():
    clean, quar, log = run([cand(source_record_id=123)])
    assert clean.empty and len(quar) == 1
    assert "source_record_id" in quar.quarantine_reason.iloc[0]
    assert (log.rule_name == log.check_name).all()


def test_flag_keeps_rows_with_bad_coordinates():
    clean, quar, _ = run([
        cand(url="https://a.pk/1", lat=95.0, lon=67.0),     # impossible
        cand(url="https://a.pk/2", lat=51.5, lon=-0.1),     # valid, but not Pakistan
        cand(url="https://a.pk/3", lat=24.86, lon=67.0),    # Karachi
        cand(url="https://a.pk/4", country_tag="ru"),
    ])
    assert len(quar) == 0 and len(clean) == 4
    f = dict(zip(clean.url, clean.dq_flags))
    assert "IMPOSSIBLE_COORD" in f["https://a.pk/1"] and np.isnan(clean[clean.url == "https://a.pk/1"].lat.iloc[0])
    assert "COORD_OUTSIDE_PK" in f["https://a.pk/2"]
    assert f["https://a.pk/3"] == ""
    assert "NOT_PK_TAG" in f["https://a.pk/4"]
    assert (clean.verification_status == "unverified").all()


def test_every_action_type_is_logged_and_columns_are_complete():
    clean, _, log = run([cand(city="KHI", urgency_score=np.nan),
                         cand(url="https://a.pk/2", urgency_score=6.0)])
    assert {"fix", "fill", "flag", "pass"} <= set(log.action)
    assert {"rule_name", "reason", "logged_at_utc", "rows_affected"} <= set(log.columns)
    assert log.rule_name.notna().all() and log.reason.str.len().gt(0).all()
    assert set(SILVER_COLS) - set(clean.columns) <= {"region_group"}


def test_canonical_url_edge_cases():
    assert canonical_url("   ") is None and canonical_url(None) is None
    assert canonical_url("ftp://x.com/a") == "ftp://x.com/a"


def test_distribution_baseline_is_persisted_once_and_flags_large_drift():
    con = db.connect(":memory:")
    first = DataQualityAgent(con, "baseline_run")
    clean, _ = first.run(pd.DataFrame([cand()]))
    first.check_distribution(clean)
    first.persist_distribution_baseline()
    baseline_count = con.execute("SELECT count(*) FROM dq_distribution_baseline").fetchone()[0]
    assert baseline_count == 3

    second = DataQualityAgent(con, "drift_run")
    rows = [cand(url=f"https://a.pk/{i}", issue_type="Electricity") for i in range(4)]
    rows.append(cand(url="https://a.pk/water", issue_type="Water"))
    shifted, _ = second.run(pd.DataFrame(rows))
    second.check_distribution(shifted)
    drift = [row for row in second.logs if row["check_name"].startswith("distribution_drift:issue_type:")]
    assert drift and all(row["action"] == "flag" for row in drift)
    assert con.execute("SELECT count(*) FROM dq_distribution_baseline").fetchone()[0] == baseline_count
    assert all(row["rule_name"] == row["check_name"] and row["reason"] for row in second.logs)

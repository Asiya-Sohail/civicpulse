from civicpulse import db
from civicpulse.ingest import gdelt
from civicpulse.pipeline import run_build

from tests.helpers import gdelt_line, write_gdelt_zip, write_newsagent


def test_gdelt_parse_keeps_only_pakistan(tmp_path):
    z = write_gdelt_zip(tmp_path / "20260301061500.export.CSV.zip", [
        gdelt_line(1, "https://dawn.com/karachi-load-shedding", "Karachi, Sindh, Pakistan", 24.86, 67.0),
        gdelt_line(2, "https://bbc.com/london-traffic", "London, England, United Kingdom", 51.5, -0.1, country="UK"),
    ])
    df = gdelt.parse_zip(z)
    assert list(df.GLOBALEVENTID) == ["1"] and df.shape[1] == 61


def test_slot_stamps_sampling():
    from datetime import date
    s = gdelt.slot_stamps(date(2026, 3, 1), date(2026, 3, 2), 2)
    assert s == ["20260301000000", "20260301120000", "20260302000000", "20260302120000"]


def test_end_to_end_rebuild_is_deterministic(tmp_path):
    bronze, cache, mani, dbp = tmp_path / "bronze", tmp_path / "cache", tmp_path / "m.csv", tmp_path / "w.duckdb"
    write_gdelt_zip(bronze / "gdelt" / "20260301061500.export.CSV.zip", [
        gdelt_line(1, "https://dawn.com/karachi-load-shedding-hits-city", "Karachi, Sindh, Pakistan", 24.86, 67.0),
        gdelt_line(2, "https://tribune.com.pk/lahore-water-shortage?utm_source=a", "Lahore, Punjab, Pakistan", 31.5, 74.3),
        gdelt_line(3, "https://dawn.com/budget-talks", "Islamabad, Pakistan", 33.7, 73.0),   # not a civic issue
    ])
    write_newsagent(bronze / "newsagent" / "20260301T070000Z_feed.json", [
        {"title": "K-Electric load shedding in KHI", "url": "https://dawn.com/karachi-load-shedding-hits-city/",
         "published_at": "2026-03-01T06:20:00Z", "urgency": 7, "country": "pk"},
        {"title": "Traffic jam on Shahrah-e-Faisal", "url": "https://geo.tv/traffic-jam", "published_at": None,
         "country": "pk"},
    ])
    from civicpulse.ingest import manifest
    manifest.append_manifest([{
        "file_path": "newsagent/20260301T070000Z_feed.json", "source": "newsagent", "source_url": "t",
        "search_query": "country=pk", "fetch_timestamp_utc": "2026-03-01T07:00:00Z", "sha256": "abc",
        "bytes": 1, "license_terms": "test"}], mani)     # old pull time -> feed must be flagged STALE
    first = run_build(dbp, bronze, cache, mani)
    assert first["bronze_gdelt"] == 3 and first["bronze_newsagent"] == 2
    con = db.connect(dbp)
    ev = con.execute("SELECT row_id, source, city, issue_type FROM gold.issue_events ORDER BY row_id").df()
    assert len(ev) == 3 and ev.row_id.is_unique          # duplicate Dawn link collapsed, civic rows only
    assert "Karachi" in set(ev.city) and "Lahore" in set(ev.city)
    ids1 = list(ev.row_id)
    imputed = con.execute("SELECT count(*) FROM silver.articles WHERE is_imputed").fetchone()[0]
    assert imputed >= 1
    assert con.execute("SELECT count(*) FROM quality_log WHERE run_id = ?", [first["run_id"]]).fetchone()[0] > 10
    con.close()

    second = run_build(dbp, bronze, cache, mani)         # rebuild from scratch: same row_ids
    con = db.connect(dbp)
    ids2 = [r[0] for r in con.execute("SELECT row_id FROM gold.issue_events ORDER BY row_id").fetchall()]
    assert ids1 == ids2
    assert con.execute("SELECT count(DISTINCT run_id) FROM quality_log").fetchone()[0] == 2  # log history kept
    assert con.execute("SELECT status FROM gold.data_freshness WHERE source='newsagent'").fetchone()[0] == "STALE"

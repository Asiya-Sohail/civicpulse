from civicpulse import db
from civicpulse.pipeline import run_build

from tests.helpers import manifest_row, write_freenewsapi


def test_freenewsapi_article_schema_is_mapped(tmp_path):
    """Article fields follow the FreeNewsAPI response schema."""
    path = write_freenewsapi(tmp_path / "bronze" / "freenewsapi" / "20260301T070000Z_feed.json", [{
        "id": "abc123", "title": "Water shortage hits Karachi", "sitename": "Dawn",
        "host": "www.dawn.com", "url": "https://www.dawn.com/news/1",
        "published_at": "2026-03-01T06:30:00Z", "crawled_at": "2026-03-01T06:40:00Z",
        "country": "PK", "categories": ["water"],
    }])
    from civicpulse.ingest.manifest import append_manifest
    append_manifest([manifest_row(path)], tmp_path / "m.csv")
    run_build(tmp_path / "w.duckdb", tmp_path / "bronze", tmp_path / "cache", tmp_path / "m.csv")
    con = db.connect(tmp_path / "w.duckdb")
    r = con.execute("SELECT source_record_id, title, url, publisher, published_utc, crawled_utc, issue_type, city, country_tag "
                    "FROM silver.articles").fetchone()
    assert r[0] == "abc123" and r[1] == "Water shortage hits Karachi"
    assert r[2] == "https://www.dawn.com/news/1" and r[3] == "Dawn"
    assert str(r[4]).startswith("2026-03-01 06:30")
    assert str(r[5]).startswith("2026-03-01 06:40")
    assert r[6] == "Water" and r[7] == "Karachi" and r[8] == "PK"

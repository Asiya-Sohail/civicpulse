from civicpulse import db
from civicpulse.pipeline import run_build

from tests.helpers import write_newsagent


def test_real_newsagent_schema_is_mapped(tmp_path):
    """Article shape taken from the provider's published OpenAPI example (field names only)."""
    write_newsagent(tmp_path / "bronze" / "newsagent" / "20260301T070000Z_feed.json", [{
        "id": "abc123", "title": "Water shortage hits Karachi", "source": "Dawn",
        "link": "https://www.dawn.com/news/1", "fetched_at": "2026-03-01T06:30:00Z",
        "urgency_score": 6, "country_tags": ["pk"], "topic_tags": ["water"], "event_type": "other",
    }])
    run_build(tmp_path / "w.duckdb", tmp_path / "bronze", tmp_path / "cache", tmp_path / "m.csv")
    con = db.connect(tmp_path / "w.duckdb")
    r = con.execute("SELECT url, published_utc, urgency_score, is_imputed, issue_type, city, country_tag "
                    "FROM silver.articles").fetchone()
    assert r[0] == "https://www.dawn.com/news/1" and str(r[1]).startswith("2026-03-01 06:30")
    assert r[2] == 6 and r[3] is False and r[4] == "Water" and r[5] == "Karachi" and "pk" in r[6]

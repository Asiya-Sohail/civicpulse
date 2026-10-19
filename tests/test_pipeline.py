from civicpulse import config, db
from civicpulse.ingest import gdelt
from civicpulse.pipeline import run_build

from tests.helpers import gdelt_line, manifest_row, write_freenewsapi, write_gdelt_zip


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


def test_four_exports_per_day_are_evenly_sampled():
    from datetime import date
    assert gdelt.slot_stamps(date(2026, 3, 1), date(2026, 3, 1), 4) == [
        "20260301000000", "20260301060000", "20260301120000", "20260301180000"
    ]


def test_configured_historical_sample_is_at_least_two_years_and_four_exports_daily():
    assert config.GDELT_DAYS_BACK >= 730
    assert config.GDELT_PER_DAY >= 4


def test_gdelt_parse_cache_invalidates_when_raw_hash_changes(tmp_path):
    from civicpulse.ingest.manifest import sha256_file

    path = tmp_path / "20260301061500.export.CSV.zip"
    cache = tmp_path / "cache"
    write_gdelt_zip(path, [
        gdelt_line(1, "https://dawn.com/karachi-load-shedding", "Karachi, Sindh, Pakistan", 24.86, 67.0)
    ])
    first = gdelt.parse_zip_cached(path, cache, sha256_file(path))
    write_gdelt_zip(path, [
        gdelt_line(2, "https://dawn.com/lahore-water-shortage", "Lahore, Punjab, Pakistan", 31.5, 74.3)
    ])
    second = gdelt.parse_zip_cached(path, cache, sha256_file(path))
    assert list(first.GLOBALEVENTID) == ["1"]
    assert list(second.GLOBALEVENTID) == ["2"]


def test_legacy_pipeline_run_source_column_is_migrated():
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE TABLE pipeline_runs (
               run_id VARCHAR, started_utc TIMESTAMP, finished_utc TIMESTAMP, status VARCHAR,
               bronze_newsagent BIGINT, bronze_gdelt BIGINT, silver_rows BIGINT,
               quarantined_rows BIGINT, gold_events BIGINT, gold_hourly BIGINT, error VARCHAR)"""
    )
    db.ensure_meta(con)
    columns = {row[0] for row in con.execute("DESCRIBE pipeline_runs").fetchall()}
    assert "bronze_freenewsapi" in columns and "bronze_newsagent" not in columns
    con.close()


def test_legacy_quality_log_migration_adds_explicit_rule_and_reason():
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE TABLE quality_log (
               run_id VARCHAR, logged_at_utc TIMESTAMP, layer VARCHAR, table_name VARCHAR,
               check_name VARCHAR, action VARCHAR, rows_affected BIGINT, detail VARCHAR)"""
    )
    con.execute(
        """INSERT INTO quality_log VALUES
           ('old-run', now(), 'silver', 'silver.articles', 'duplicate_links', 'fix', 2, 'removed duplicates')"""
    )
    db.ensure_meta(con)
    rule, reason = con.execute(
        "SELECT rule_name, reason FROM quality_log WHERE run_id='old-run'"
    ).fetchone()
    assert rule == "duplicate_links" and reason == "removed duplicates"
    con.close()


def test_end_to_end_rebuild_is_deterministic(tmp_path):
    bronze, cache, mani, dbp = tmp_path / "bronze", tmp_path / "cache", tmp_path / "m.csv", tmp_path / "w.duckdb"
    gdelt_path = write_gdelt_zip(bronze / "gdelt" / "20260301061500.export.CSV.zip", [
        gdelt_line(1, "https://dawn.com/karachi-load-shedding-hits-city", "Karachi, Sindh, Pakistan", 24.86, 67.0),
        gdelt_line(2, "https://tribune.com.pk/lahore-water-shortage?utm_source=a", "Lahore, Punjab, Pakistan", 31.5, 74.3),
        gdelt_line(3, "https://dawn.com/budget-talks", "Islamabad, Pakistan", 33.7, 73.0),   # not a civic issue
    ])
    freenewsapi_path = write_freenewsapi(bronze / "freenewsapi" / "20260301T070000Z_feed.json", [
        {"id": "free-1", "title": "K-Electric load shedding in KHI", "url": "https://dawn.com/karachi-load-shedding-hits-city/",
         "published_at": "2026-03-01T06:20:00Z", "sitename": "Dawn", "country": "PK"},
        {"id": "free-2", "title": "Traffic jam on Shahrah-e-Faisal", "url": "https://geo.tv/traffic-jam",
         "published_at": None, "host": "geo.tv", "country": "PK"},
    ])
    from civicpulse.ingest import manifest
    manifest.append_manifest(
        [manifest_row(gdelt_path), manifest_row(freenewsapi_path)], mani
    )  # old pull time -> feed must be flagged STALE
    first = run_build(dbp, bronze, cache, mani)
    assert first["bronze_gdelt"] == 3 and first["bronze_freenewsapi"] == 2
    assert first["weather_rows"] == 0
    con = db.connect(dbp)
    assert con.execute(
        """
        SELECT count(*)
        FROM information_schema.tables
        WHERE table_schema = 'gold'
          AND table_name = 'weather_hourly_features'
        """
    ).fetchone()[0] == 1

    assert con.execute(
        """
        SELECT count(*)
        FROM gold.hourly_issue_counts_with_weather
        """
    ).fetchone()[0] == con.execute(
        """
        SELECT count(*)
        FROM gold.hourly_issue_counts
        """
    ).fetchone()[0]
    ev = con.execute("SELECT row_id, source, city, issue_type FROM gold.issue_events ORDER BY row_id").df()
    assert len(ev) == 3 and ev.row_id.is_unique          # duplicate Dawn link collapsed, civic rows only
    assert "Karachi" in set(ev.city) and "Lahore" in set(ev.city)
    trace = con.execute(
        "SELECT bronze_file_path, bronze_file_sha256, manifest_id FROM silver.articles"
    ).df()
    assert trace.notna().all().all() and trace.bronze_file_sha256.str.len().eq(64).all()
    assert con.execute(
        "SELECT count(*) FROM gold.issue_events WHERE manifest_id IS NULL OR bronze_file_path IS NULL"
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT count(*) FROM gold.hourly_issue_counts WHERE manifest_ids IS NULL OR bronze_file_paths IS NULL"
    ).fetchone()[0] == 0
    baseline_rows, baseline_dimensions = con.execute(
        """SELECT count(*), count(DISTINCT dimension) FROM dq_distribution_baseline
           WHERE baseline_run_id = ?""",
        [first["run_id"]],
    ).fetchone()
    assert baseline_rows >= 3 and baseline_dimensions == 3
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
    assert con.execute("SELECT status FROM gold.data_freshness WHERE source='freenewsapi'").fetchone()[0] == "STALE"


def test_manifest_inventory_detects_unmanifested_missing_and_modified_files(tmp_path):
    import pytest
    from civicpulse.ingest import manifest

    bronze, mani = tmp_path / "bronze", tmp_path / "manifest.csv"
    path = write_freenewsapi(bronze / "freenewsapi" / "feed.json", [])
    with pytest.raises(manifest.ManifestIntegrityError, match="Unmanifested"):
        manifest.validate_bronze_inventory(bronze, mani)

    manifest.append_manifest([manifest_row(path)], mani)
    assert len(manifest.validate_bronze_inventory(bronze, mani)) == 1

    path.write_text('{"results":[],"changed":true}', encoding="utf-8")
    with pytest.raises(manifest.ManifestIntegrityError, match="SHA-256 mismatch"):
        manifest.validate_bronze_inventory(bronze, mani)

    path.unlink()
    with pytest.raises(manifest.ManifestIntegrityError, match="missing from Bronze"):
        manifest.validate_bronze_inventory(bronze, mani)


def test_raw_volume_gate_fails_before_silver_and_baseline(tmp_path):
    import pytest

    db_path = tmp_path / "warehouse.duckdb"
    with pytest.raises(RuntimeError, match="below required minimum 1"):
        run_build(
            db_path, tmp_path / "bronze", tmp_path / "cache", tmp_path / "manifest.csv",
            minimum_raw_rows=1,
        )
    con = db.connect(db_path)
    assert con.execute(
        "SELECT status FROM pipeline_runs ORDER BY started_utc DESC LIMIT 1"
    ).fetchone()[0] == "FAILED"
    assert con.execute(
        "SELECT count(*) FROM quality_log WHERE rule_name='raw_dataset_volume' AND action='flag'"
    ).fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM dq_distribution_baseline").fetchone()[0] == 0

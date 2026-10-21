from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import duckdb


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_gold_to_supabase.py"
_SPEC = importlib.util.spec_from_file_location("sync_gold_to_supabase", _SCRIPT)
sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync)


def test_timestamp_serialization_marks_utc_pkt_and_null_values():
    assert sync._serialize(datetime(2026, 1, 2), "hour_utc") == "2026-01-02T00:00:00+00:00"
    assert sync._serialize(datetime(2026, 1, 2), "hour_pkt_display") == "2026-01-02T00:00:00+05:00"
    assert sync._serialize(None, "temperature_2m") is None


def test_supabase_sync_batches_rows_and_uses_stable_conflict_key(monkeypatch, tmp_path):
    database = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE SCHEMA gold")
    con.execute("CREATE TABLE gold.hourly_issue_counts (row_id VARCHAR, city VARCHAR, n_reports BIGINT)")
    con.execute("INSERT INTO gold.hourly_issue_counts VALUES ('h_1', 'Karachi', 4), ('h_2', 'Lahore', 3), ('h_3', 'Quetta', 2)")
    con.close()
    monkeypatch.setattr(sync.config, "DB_PATH", database)

    class Response:
        ok = True
        text = ""

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    total = sync._upsert_table(
        session, "https://example.supabase.co", "secret", "hourly_issue_counts",
        "row_id", True, 2,
    )

    assert total == 3
    assert [len(json.loads(call[1]["data"])) for call in session.calls] == [2, 1]
    assert all(call[1]["params"]["on_conflict"] == "row_id" for call in session.calls)
    assert all("resolution=merge-duplicates" in call[1]["headers"]["Prefer"] for call in session.calls)


def test_supabase_sync_skips_missing_optional_table(monkeypatch, tmp_path):
    database = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE SCHEMA gold")
    con.close()
    monkeypatch.setattr(sync.config, "DB_PATH", database)

    class Session:
        def post(self, *args, **kwargs):
            raise AssertionError("missing optional table should not upload")

    assert sync._upsert_table(Session(), "https://example.supabase.co", "secret", "poisson_forecasts", "run_id", False, 5) == 0


def test_sync_cli_requires_credentials(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

    assert sync.main() == 2

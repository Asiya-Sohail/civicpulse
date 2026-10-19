import pytest

from civicpulse import cli


def test_backfill_builds_historical_data(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_fetch_gdelt", lambda: {"downloaded": 3, "cached": 2, "missing": 1, "failed": 0})
    monkeypatch.setattr(cli.pipeline, "run_build", lambda: {"bronze_gdelt": 5, "gold_events": 2})
    monkeypatch.setattr(cli, "_print_build", lambda stats: called.append(stats))

    cli._backfill_gdelt()

    assert called == [{"bronze_gdelt": 5, "gold_events": 2}]


def test_backfill_does_not_build_after_failed_download(monkeypatch):
    monkeypatch.setattr(cli, "_fetch_gdelt", lambda: {"downloaded": 3, "cached": 2, "missing": 1, "failed": 1})
    monkeypatch.setattr(cli.pipeline, "run_build", lambda: pytest.fail("build must not run for failed backfill"))

    with pytest.raises(RuntimeError, match="1 failed downloads"):
        cli._backfill_gdelt()

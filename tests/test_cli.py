import pytest

from civicpulse import cli


def test_backfill_builds_historical_data(monkeypatch, tmp_path):
    called = []
    marker = tmp_path / "history-complete"
    monkeypatch.setattr(cli.config, "GDELT_BACKFILL_MARKER", marker)
    monkeypatch.setattr(cli, "_fetch_gdelt", lambda: {"downloaded": 3, "cached": 2, "missing": 1, "failed": 0})
    monkeypatch.setattr(
        cli.pipeline, "run_build",
        lambda **kwargs: {"bronze_gdelt": 5, "gold_events": 2},
    )
    monkeypatch.setattr(cli, "_print_build", lambda stats: called.append(stats))

    cli._backfill_gdelt()

    assert called == [{"bronze_gdelt": 5, "gold_events": 2}]
    assert marker.read_text(encoding="utf-8").startswith("gdelt_per_day=")


def test_backfill_does_not_build_after_failed_download(monkeypatch):
    monkeypatch.setattr(cli, "_fetch_gdelt", lambda: {"downloaded": 3, "cached": 2, "missing": 1, "failed": 1})
    monkeypatch.setattr(cli.pipeline, "run_build", lambda: pytest.fail("build must not run for failed backfill"))

    with pytest.raises(RuntimeError, match="1 failed downloads"):
        cli._backfill_gdelt()


def test_data_runs_backfill_when_clean_or_density_marker_is_stale(monkeypatch, tmp_path):
    marker = tmp_path / "history-complete"
    monkeypatch.setattr(cli.config, "GDELT_BACKFILL_MARKER", marker)
    monkeypatch.setattr(cli.config, "GDELT_PER_DAY", 4)
    calls = []
    monkeypatch.setattr(cli, "_backfill_gdelt", lambda: calls.append("backfill"))

    cli._ensure_historical_backfill()
    assert calls == ["backfill"]

    expected = (
        f"gdelt_per_day={cli.config.GDELT_PER_DAY};"
        f"days_back={cli.config.GDELT_DAYS_BACK}\n"
    )
    marker.write_text(expected, encoding="utf-8")
    cli._ensure_historical_backfill()
    assert calls == ["backfill"]

    marker.write_text("gdelt_per_day=2;days_back=740\n", encoding="utf-8")
    cli._ensure_historical_backfill()
    assert calls == ["backfill", "backfill"]


def test_data_command_bootstraps_history_without_manual_steps(monkeypatch, tmp_path):
    import sys

    marker = tmp_path / "history-complete"
    monkeypatch.setattr(cli.config, "GDELT_BACKFILL_MARKER", marker)
    calls = []

    def backfill():
        calls.append("backfill")
        marker.write_text(
            f"gdelt_per_day={cli.config.GDELT_PER_DAY};"
            f"days_back={cli.config.GDELT_DAYS_BACK}\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(cli, "_backfill_gdelt", backfill)
    monkeypatch.setattr(cli, "_fetch_freenewsapi", lambda: calls.append("freenewsapi") or True)
    monkeypatch.setattr(
        cli.gdelt, "fetch_recent",
        lambda **kwargs: calls.append("gdelt_recent") or {
            "downloaded": 1, "cached": 0, "missing": 0, "failed": 0,
        },
    )
    monkeypatch.setattr(
        cli.pipeline, "run_build",
        lambda **kwargs: calls.append(("build", kwargs)) or {"gold_events": 1},
    )
    monkeypatch.setattr(cli, "_print_build", lambda stats: None)
    monkeypatch.setattr(sys, "argv", ["civicpulse", "data"])

    cli.main()

    assert calls[:3] == ["backfill", "freenewsapi", "gdelt_recent"]
    assert calls[3] == ("build", {"minimum_raw_rows": cli.config.DATASET_RAW_ROW_TARGET})

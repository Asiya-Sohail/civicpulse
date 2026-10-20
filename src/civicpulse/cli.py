from __future__ import annotations

import argparse
from datetime import date

from civicpulse import config, db, pipeline
from civicpulse.ingest import freenewsapi, gdelt, open_meteo
from civicpulse.modeling import baseline, poisson_model


def _fetch_freenewsapi() -> bool:
    try:
        paths = freenewsapi.fetch_feed()
        print(f"FreeNewsAPI: saved {len(paths)} raw page(s)")
        return True
    except RuntimeError as e:
        print(f"FreeNewsAPI fetch failed: {type(e).__name__}: {e}")
        return False


def _fetch_gdelt(args=None) -> dict[str, int]:
    start, end = gdelt.default_window()
    per_day = config.GDELT_PER_DAY
    max_files = None
    if args is not None:
        start = date.fromisoformat(args.start) if args.start else start
        end = date.fromisoformat(args.end) if args.end else end
        per_day = args.per_day or per_day
        max_files = args.max_files
    n = len(gdelt.slot_stamps(start, end, per_day))
    print(f"GDELT: {start} -> {end}, {per_day}/day = {n} files (resumable; existing files are skipped)")
    counts = gdelt.fetch_range(start, end, per_day, max_files=max_files)
    print("GDELT:", counts)
    return counts


def _backfill_gdelt() -> None:
    """Fetch the configured historical GDELT sample and build all available raw data to Gold."""
    counts = _fetch_gdelt()
    if counts["failed"]:
        raise RuntimeError(f"GDELT historical backfill has {counts['failed']} failed downloads")
    print("Building Bronze, Silver, and Gold from all downloaded historical and live raw files.")
    _print_build(pipeline.run_build(minimum_raw_rows=config.DATASET_RAW_ROW_TARGET))
    config.GDELT_BACKFILL_MARKER.parent.mkdir(parents=True, exist_ok=True)
    config.GDELT_BACKFILL_MARKER.write_text(
        f"gdelt_per_day={config.GDELT_PER_DAY};days_back={config.GDELT_DAYS_BACK}\n",
        encoding="utf-8",
    )


def _ensure_historical_backfill() -> None:
    expected = f"gdelt_per_day={config.GDELT_PER_DAY};days_back={config.GDELT_DAYS_BACK}\n"
    marker = config.GDELT_BACKFILL_MARKER
    if not marker.exists() or marker.read_text(encoding="utf-8") != expected:
        print("Historical GDELT backfill is missing or uses a different sample density.")
        _backfill_gdelt()


def _print_build(stats: dict) -> None:
    print("Build complete:")
    for k, v in stats.items():
        print(f"  {k}: {v}")


def _stats() -> None:
    con = db.connect()
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    bf, bg = q("SELECT count(*) FROM bronze.freenewsapi_raw"), q("SELECT count(*) FROM bronze.gdelt_events_raw")
    rows = bf + bg
    docs = q("SELECT count(DISTINCT url) FROM silver.articles")
    print(f"bronze rows: freenewsapi={bf:,} gdelt={bg:,}")
    print(f"silver articles={q('SELECT count(*) FROM silver.articles'):,} "
          f"quarantined={q('SELECT count(*) FROM silver.quarantine'):,}")
    print(f"gold issue_events={q('SELECT count(*) FROM gold.issue_events'):,} "
          f"hourly_counts={q('SELECT count(*) FROM gold.hourly_issue_counts'):,}")
    ok = docs >= config.MIN_DOCS or rows >= config.MIN_ROWS
    print(f"volume rule (>= {config.MIN_DOCS:,} docs OR >= {config.MIN_ROWS:,} rows): "
          f"docs={docs:,} rows={rows:,} -> {'MET' if ok else 'NOT MET'}")
    print(f"raw dataset target (>= {config.DATASET_RAW_ROW_TARGET:,} rows): "
          f"{'MET' if rows >= config.DATASET_RAW_ROW_TARGET else 'NOT MET'}")
    print("\nLatest DQ summary:")
    print(con.execute("SELECT * FROM gold.dq_summary ORDER BY 1").df().to_string(index=False))
    print("\nFreshness:")
    print(con.execute("SELECT * FROM gold.data_freshness").df().to_string(index=False))

def _evaluate_model() -> None:
    """Retrain and evaluate the explainable model on the locked test period."""
    con = db.connect()

    try:
        selected = poisson_model.select_poisson_alpha(
            con=con,
            cutoff_utc=config.FORECAST_CUTOFF_UTC,
        )

        alpha = float(selected["selected_alpha"])

        result = poisson_model.evaluate_poisson_model(
            con=con,
            cutoff_utc=config.FORECAST_CUTOFF_UTC,
            alpha=alpha,
        )

        predictions = poisson_model.add_poisson_prediction_intervals(
            result["predictions"]
        )

        grouped_errors = poisson_model.summarize_errors_by_group(
            predictions
        )

        baseline_result = baseline.weekly_seasonal_naive(
            con=con,
            cutoff_utc=config.FORECAST_CUTOFF_UTC,
            lag_hours=config.FORECAST_LAG_HOURS,
        )

        baseline_test = baseline_result[
            baseline_result["split"] == "test"
        ]

        baseline_mae = (
            baseline_test["actual"]
            - baseline_test["prediction"]
        ).abs().mean()

        actual_event_predictions = predictions[
            predictions["n_reports"] > 0
        ]

        actual_event_baseline = baseline_test[
            baseline_test["actual"] > 0
        ]

        print("Model evaluation complete:")
        print(f"  selected_alpha: {alpha}")
        print(f"  cutoff_utc: {config.FORECAST_CUTOFF_UTC}")
        print(f"  test_rows: {result['test_rows']}")
        print(f"  poisson_full_grid_mae: {result['mae']}")
        print(f"  baseline_full_grid_mae: {baseline_mae}")
        print(
            "  poisson_actual_event_mae: "
            f"{actual_event_predictions['absolute_error'].mean()}"
        )
        print(
            "  baseline_actual_event_mae: "
            f"{(
                actual_event_baseline["actual"]
                - actual_event_baseline["prediction"]
            ).abs().mean()}"
        )
        print("  top_group_errors:")
        print(grouped_errors.head(10).to_string(index=False))

    finally:
        con.close()
def main() -> None:
    p = argparse.ArgumentParser(prog="civicpulse")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("data", help="ensure historical backfill, fetch current sources, then rebuild all layers")
    sub.add_parser("history", help="download sampled historical GDELT data (one-time/backfill)")
    sub.add_parser("backfill", help="download historical GDELT sample and build all raw data to Gold")
    sub.add_parser("build", help="rebuild the warehouse from files already on disk")
    sub.add_parser("fetch-freenewsapi", help="fetch recent Pakistan articles from FreeNewsAPI")
    sub.add_parser("probe-freenewsapi", help="check FreeNewsAPI connectivity and response shape")
    g = sub.add_parser("fetch-gdelt")
    g.add_argument("--start"), g.add_argument("--end")
    g.add_argument("--per-day", type=int), g.add_argument("--max-files", type=int)

    w = sub.add_parser("fetch-weather")
    w.add_argument("--city", required=True)
    w.add_argument("--latitude", required=True, type=float)
    w.add_argument("--longitude", required=True, type=float)
    w.add_argument("--start", required=True)
    w.add_argument("--end", required=True)

    sub.add_parser("stats")
    sub.add_parser(
    "evaluate-model",
    help="retrain and evaluate the model on the locked test period",
)
    a = p.parse_args()

    if a.cmd == "data":
        # The marker prevents hourly full-window scans and is invalidated when the sample density changes.
        _ensure_historical_backfill()
        if not _fetch_freenewsapi():
            raise RuntimeError("FreeNewsAPI fetch failed; refusing to report a complete data build")
        recent = gdelt.fetch_recent(hours=4)
        print("GDELT recent slots:", recent)
        if recent["failed"]:
            raise RuntimeError(f"GDELT recent fetch has {recent['failed']} failed download(s)")
        _print_build(pipeline.run_build(minimum_raw_rows=config.DATASET_RAW_ROW_TARGET))
    elif a.cmd == "history":
        _fetch_gdelt()
    elif a.cmd == "backfill":
        _backfill_gdelt()
    elif a.cmd == "build":
        _print_build(pipeline.run_build(minimum_raw_rows=config.DATASET_RAW_ROW_TARGET))
    elif a.cmd == "fetch-freenewsapi":
        raise SystemExit(0 if _fetch_freenewsapi() else 1)
    elif a.cmd == "probe-freenewsapi":
        freenewsapi.probe()
    elif a.cmd == "fetch-gdelt":
        _fetch_gdelt(a)
    elif a.cmd == "fetch-weather":
        path = open_meteo.fetch_historical_weather(
            start_date=date.fromisoformat(a.start),
            end_date=date.fromisoformat(a.end),
            city=a.city,
            latitude=a.latitude,
            longitude=a.longitude,
            output_dir=config.OPEN_METEO_BRONZE_DIR,
            manifest_path=config.MANIFEST_PATH,
        )
        print(f"Open-Meteo: saved {path}")
    elif a.cmd == "stats":
        _stats()
    elif a.cmd == "evaluate-model":
        _evaluate_model()


if __name__ == "__main__":
    main()

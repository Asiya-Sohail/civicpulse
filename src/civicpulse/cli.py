from __future__ import annotations

import argparse
from datetime import date

from civicpulse import config, db, pipeline
from civicpulse.ingest import gdelt, newsagent


def _fetch_newsagent() -> bool:
    try:
        path = newsagent.fetch_feed()
        print(f"NewsAgent: saved {path.name}")
        return True
    except newsagent.NewsAgentConfigError as e:
        print(f"NewsAgent skipped: {e}")
        return False


def _fetch_gdelt(args=None) -> None:
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
    print("GDELT:", gdelt.fetch_range(start, end, per_day, max_files=max_files))


def _print_build(stats: dict) -> None:
    print("Build complete:")
    for k, v in stats.items():
        print(f"  {k}: {v}")


def _stats() -> None:
    con = db.connect()
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    bn, bg = q("SELECT count(*) FROM bronze.newsagent_raw"), q("SELECT count(*) FROM bronze.gdelt_events_raw")
    rows = bn + bg
    docs = q("SELECT count(DISTINCT url) FROM silver.articles")
    print(f"bronze rows: newsagent={bn:,} gdelt={bg:,}")
    print(f"silver articles={q('SELECT count(*) FROM silver.articles'):,} "
          f"quarantined={q('SELECT count(*) FROM silver.quarantine'):,}")
    print(f"gold issue_events={q('SELECT count(*) FROM gold.issue_events'):,} "
          f"hourly_counts={q('SELECT count(*) FROM gold.hourly_issue_counts'):,}")
    ok = docs >= config.MIN_DOCS or rows >= config.MIN_ROWS
    print(f"volume rule (>= {config.MIN_DOCS:,} docs OR >= {config.MIN_ROWS:,} rows): "
          f"docs={docs:,} rows={rows:,} -> {'MET' if ok else 'NOT MET'}")
    print("\nLatest DQ summary:")
    print(con.execute("SELECT * FROM gold.dq_summary ORDER BY 1").df().to_string(index=False))
    print("\nFreshness:")
    print(con.execute("SELECT * FROM gold.data_freshness").df().to_string(index=False))


def main() -> None:
    p = argparse.ArgumentParser(prog="civicpulse")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("data", help="fetch both sources, then rebuild bronze/silver/gold + DQ gate")
    sub.add_parser("build", help="rebuild the warehouse from files already on disk")
    sub.add_parser("fetch-newsagent")
    sub.add_parser("probe-newsagent")
    g = sub.add_parser("fetch-gdelt")
    g.add_argument("--start"), g.add_argument("--end")
    g.add_argument("--per-day", type=int), g.add_argument("--max-files", type=int)
    sub.add_parser("stats")
    a = p.parse_args()

    if a.cmd == "data":
        _fetch_newsagent()
        _fetch_gdelt()
        _print_build(pipeline.run_build())
    elif a.cmd == "build":
        _print_build(pipeline.run_build())
    elif a.cmd == "fetch-newsagent":
        raise SystemExit(0 if _fetch_newsagent() else 1)
    elif a.cmd == "probe-newsagent":
        newsagent.probe()
    elif a.cmd == "fetch-gdelt":
        _fetch_gdelt(a)
    elif a.cmd == "stats":
        _stats()


if __name__ == "__main__":
    main()

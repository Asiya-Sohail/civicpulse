"""One call rebuilds the whole warehouse: bronze -> silver (+DQ gate) -> gold (+DQ gate)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from civicpulse import config, db
from civicpulse.quality.agent import DataQualityAgent
from civicpulse.warehouse import bronze, gold, silver


def run_build(
    db_path: Path | str = config.DB_PATH,
    bronze_dir: Path = config.BRONZE_DIR,
    cache_dir: Path = config.CACHE_DIR,
    manifest_path: Path = config.MANIFEST_PATH,
) -> dict:
    run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    con = db.connect(db_path)
    agent = DataQualityAgent(con, run_id)
    stats = {"run_id": run_id}
    status, error = "SUCCESS", None
    # Preserve the last successful Gold outputs until the new Gold tables pass their gate.
    gold_tables = ("issue_events", "hourly_issue_counts", "data_freshness")
    existing_gold = {
        name: con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='gold' AND table_name=?",
            [name],
        ).fetchone()[0] > 0
        for name in gold_tables
    }
    for name, exists in existing_gold.items():
        if exists:
            con.execute(f"CREATE OR REPLACE TABLE _last_good_{name} AS SELECT * FROM gold.{name}")
    try:
        b = bronze.build_bronze(con, bronze_dir, cache_dir, manifest_path)
        s = silver.build_silver(con, agent)
        g = gold.build_gold(con)
        agent.gate_gold()
        gold.build_freshness(con, agent.freshness, run_id)
        stats |= {"bronze_freenewsapi": b["freenewsapi"], "bronze_gdelt": b["gdelt"], **s, **g}
    except Exception as exc:  # the failure itself must be in the audit trail
        status, error = "FAILED", f"{type(exc).__name__}: {exc}"
        # Roll back Gold only; keep the previous trusted version if the new build fails.
        for name, exists in existing_gold.items():
            if exists:
                con.execute(f"CREATE OR REPLACE TABLE gold.{name} AS SELECT * FROM _last_good_{name}")
            else:
                con.execute(f"DROP TABLE IF EXISTS gold.{name}")
        agent.log("pipeline_exception", "flag", 0, error, layer="pipeline", table="-")
        raise
    finally:
        agent.flush()
        for name in gold_tables:
            con.execute(f"DROP TABLE IF EXISTS _last_good_{name}")
        con.execute(
            "INSERT INTO pipeline_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [run_id, started, datetime.now(timezone.utc).replace(tzinfo=None), status,
             stats.get("bronze_freenewsapi"), stats.get("bronze_gdelt"), stats.get("silver"),
             stats.get("quarantined"), stats.get("gold_events"), stats.get("gold_hourly"), error],
        )
        con.close()
    return stats

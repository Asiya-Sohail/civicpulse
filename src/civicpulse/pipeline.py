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
    try:
        b = bronze.build_bronze(con, bronze_dir, cache_dir, manifest_path)
        s = silver.build_silver(con, agent)
        g = gold.build_gold(con)
        agent.gate_gold()
        gold.build_freshness(con, agent.freshness, run_id)
        stats |= {"bronze_newsagent": b["newsagent"], "bronze_gdelt": b["gdelt"], **s, **g}
    except Exception as exc:  # the failure itself must be in the audit trail
        status, error = "FAILED", f"{type(exc).__name__}: {exc}"
        agent.log("pipeline_exception", "flag", 0, error, layer="pipeline", table="-")
        raise
    finally:
        agent.flush()
        con.execute(
            "INSERT INTO pipeline_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [run_id, started, datetime.now(timezone.utc).replace(tzinfo=None), status,
             stats.get("bronze_newsagent"), stats.get("bronze_gdelt"), stats.get("silver"),
             stats.get("quarantined"), stats.get("gold_events"), stats.get("gold_hourly"), error],
        )
        con.close()
    return stats

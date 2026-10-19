# Pakistan CivicPulse - warehouse + Data Quality Agent (phase 1)

Turns Pakistani news reports into geo-tagged, *unverified-until-confirmed* public issue records
(electricity, water, mobility, municipal). See `SETUP.md` to run it and `DATA_SOURCES.md` for the declaration.

```
NewsAgent API ─┐                                          ┌─ silver.quarantine (failed hard checks)
               ├─► data/bronze (immutable files + manifest.csv) ─► DuckDB bronze ─► silver ─► gold
GDELT zips ────┘                                          │      │ DQ Agent: fix/fill/flag/quarantine ─► quality_log
                                                          └──────┴─ gold.issue_events, gold.hourly_issue_counts, gold.data_freshness
```

## Stack (all free)
| Need | Choice | Why |
|---|---|---|
| Warehouse | DuckDB (single file) | no server, fast SQL on 100k+ rows, reads parquet |
| Validation | Pandera | named checks map 1:1 to `quality_log` entries |
| Raw storage | read-only files + `manifest.csv` (SHA-256) | reproducible, auditable |
| Next phases | Prefect OSS (scheduler), LightGBM (forecast), Gradio on Hugging Face Spaces (dashboard) | free tiers |

## Layers
- **Bronze**: files exactly as received (read-only). DuckDB bronze tables are rebuilt from them.
- **Silver**: one schema for both sources; UTC timestamps (PKT only in `*_pkt_display` columns); duplicate links removed; city / issue type / service extracted by rules in `config.py`.
- **Gold**: `issue_events` (one row per reported issue) and `hourly_issue_counts` (hour x city x issue, for the model). `row_id` is a hash of the URL / grain, so it is identical on every rebuild.

## DQ agent (`src/civicpulse/quality/agent.py`)
Fix (trim, `KHI` -> Karachi, canonical URLs, dedupe) -> Fill (`is_imputed=True` + `imputed_fields`) -> Quarantine
(missing/invalid URL, timestamp out of range, urgency outside 0-10) -> Flag (unverified, impossible or out-of-Pakistan
coordinates, non-PK country tag, stale feed). Every check, including passes, is a row in `quality_log`.

## Caveats
- Imputed urgency (GDELT rows have none) is a per-issue median: it is marked, but it carries no real signal. Exclude `is_imputed` rows when judging urgency.
- Tests use tiny fixtures in `tests/` only; they are never loaded by `make data`.

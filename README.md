# Pakistan CivicPulse - warehouse + Data Quality Agent (phase 1)

Turns Pakistani news reports into geo-tagged, *unverified-until-confirmed* public issue records
(electricity, water, mobility, municipal). See `SETUP.md` to run it and `DATA_SOURCES.md` for the declaration.

```
FreeNewsAPI ───┐                                          ┌─ silver.quarantine (failed hard checks)
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
- **Silver**: one typed schema for both sources; UTC timestamps (PKT only in `*_pkt_display` columns); duplicate links removed; city / issue type / service extracted by rules in `config.py`. Every row retains its Bronze path, raw SHA-256, and manifest ID.
- **Gold**: `issue_events` (one row per reported issue) and `hourly_issue_counts` (hour x city x issue, for the model). `row_id` is a hash of the URL / grain, so it is identical on every rebuild. Event records carry their single raw-file provenance; hourly records carry the contributing manifest IDs and file paths.
- **Live source**: FreeNewsAPI keyless search, filtered to Pakistan and polled hourly with an overlapping crawl-time window. Source article IDs, titles, timestamps, publisher names, and URLs are retained; repeated URLs are deduplicated downstream.

## DQ agent (`src/civicpulse/quality/agent.py`)
Fix (trim, `KHI` -> Karachi, canonical URLs, dedupe) -> Fill (`is_imputed=True` + `imputed_fields`) -> Quarantine
(invalid required ID/URL/timestamp, timestamp out of range, urgency outside 0-10, invalid provenance) -> Flag
(unverified reports, impossible/out-of-Pakistan coordinates, missing optional fields, non-PK country tag, stale feed,
and category-share drift). The initial source/issue/city distribution baseline is persisted after the first successful
Gold build and then held fixed; a share change above 20 percentage points is flagged. Every audit record has a named
rule, action, reason, UTC timestamp, and affected-row count in `quality_log`.

## Reproducibility and volume
`make data` on a clean data directory runs the historical GDELT sample backfill, fetches current FreeNewsAPI and recent
GDELT files, verifies the manifest inventory and SHA-256 hashes, then builds and gates Bronze → Silver → Gold. A versioned
completion marker prevents the full historical download from repeating on hourly runs. Historical 404 export slots are
reported as unavailable; failed downloads stop the backfill. This requires network access to both source endpoints.
GDELT defaults to four sampled exports per day (out of 96); counts are measured from fetched raw files and are not
artificially duplicated. The configured build gate requires at least 60,000 Bronze rows and at least 730 days of
historical sampling. See [DATA_SOURCES.md](DATA_SOURCES.md) for terms, provenance, and limits.

The pre-evaluation deterministic forecasting baseline is weekly seasonal naive: predict a city's and issue type's count
for each UTC hour from the same UTC hour one week earlier (t−168 hours), treating unobserved city/issue/hour bins as
zero inside the declared observation window. Freeze a chronological holdout and compare MAE before evaluating a more
complex model. No model performance is claimed.

The requested third-party official source is not yet included. NDMA's published terms restrict use to personal,
non-commercial access and prohibit unauthorized reproduction/distribution; its `robots.txt` disallows `/api`. Do not
enable an NDMA fetch until explicit reuse permission or a suitable open licence is available. FreeNewsAPI and GDELT are
both media-derived and should not be described as truly independent primary sources.

## Caveats
- Imputed urgency (GDELT rows have none) is a per-issue median: it is marked, but it carries no real signal. Exclude `is_imputed` rows when judging urgency.
- Tests use tiny fixtures in `tests/` only; they are never loaded by `make data`.

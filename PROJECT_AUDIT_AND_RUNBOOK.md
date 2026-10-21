# CivicPulse Project Audit and End-to-End Runbook

**Audit date:** 2026-10-21  
**Purpose:** Record what is implemented, what was checked in this checkout, how to run each stage, and what must be resolved before building the Streamlit app.

## Executive result

The data pipeline, model code, RAG retrieval/generation code, and Supabase batch-sync code are present. The automated suite currently passes **58 tests**. The scheduled GitHub workflow runs tests, builds Gold, evaluates the model, creates rolling Poisson forecasts, and upserts Gold and forecast rows to Supabase.

The project is **not ready to call a complete deployed application** yet. There is no Streamlit app, and the code that answers RAG questions still reads DuckDB and Chroma from the local filesystem. It does not read Gold rows or forecasts from Supabase. Also, the local warehouse inspected for this audit has historical weather for Karachi only, while `DATA_SOURCES.md` describes 12-city coverage.

## What I checked in this checkout

### Automated tests and commands

- Full suite: **58 passed in 66.91 seconds**. The run used a project-local temporary directory because the audit environment could not create pytest's default temporary directory outside the workspace.
- `python -m civicpulse --help`: passed; the CLI exposes data collection/build, weather ingestion, stats, model evaluation/forecasting, RAG indexing/retrieval/answering.
- `python -m civicpulse stats`: completed successfully. The configured raw-data gates passed.
- `python -m civicpulse evaluate-model`: completed. It took about two minutes on this machine.
- `python -m civicpulse rag-retrieve --question "What did the Poisson model forecast for Water in Karachi?"`: could not complete because this machine could not resolve Hugging Face to check/download the embedding model. This was a DNS/network failure; it did not reach OpenRouter.
- I did not run the live data fetch, RAG website indexing, OpenRouter answer call, or Supabase sync during the audit. Those require external network access, API credentials, or remote writes.

### Local warehouse snapshot

`python -m civicpulse stats` reported:

- Bronze: 61,254 rows — FreeNewsAPI 47, GDELT 61,207.
- Silver: 13,837 articles; 0 quarantined.
- Gold: 142 issue events and 142 hourly count rows.
- Raw volume gate: **MET** (61,254 rows, above 60,000).
- Documentation volume rule: **MET** (61,254 rows, above 50,000; 13,837 distinct articles, above 5,000).
- Freshness table showed FreeNewsAPI as `FRESH` at the time of the local run.
- Gold weather: 17,736 hourly rows, all for **Karachi** in this checkout.
- Saved Poisson forecasts: 3,408 rows at inspection time.

The DQ action totals are counts of rule actions and may overlap; they are not counts of unique bad records.

### Model evaluation observed in this audit

The current evaluation command selected alpha `0.01`, used cutoff `2026-04-06T18:00:00Z`, and produced 986,336 test-grid rows. Full-grid MAE was approximately `0.00007116` for Poisson and `0.00005678` for the weekly seasonal baseline. The baseline remains better on this evaluation; the project should describe Poisson outputs as experimental report-count estimates, not as a proven improvement.

These values differ from the committed numbers in `MODEL_CARD.md` and `MODEL_EVALUATION.md` (which list 996,824 grid rows and slightly different MAE values). Reconcile which exact data snapshot is the locked evaluation before presenting metrics in a dashboard or submission.

## Audit findings before Streamlit

### Resolve these first

1. **Connect the future app to persistent data.** The GitHub workflow writes Gold and forecasts to Supabase, but RAG's `_gold_context` and `_poisson_forecast_context` query local DuckDB. A Streamlit deployment cannot see the GitHub runner's local DuckDB file. Implement a Supabase read path for the app and decide how it will access a persistent vector index.
2. **Reconcile actual weather coverage.** The local warehouse has only Karachi weather (17,736 rows), but `DATA_SOURCES.md` claims 12 cities and 212,880 rows. The GitHub workflow fetches a fixed historical range and does not refresh historical weather hourly. The model can impute missing weather; that does not mean the weather source was successfully collected for all locations.
3. **Fix evaluation documentation.** Re-run and freeze the intended evaluation snapshot, then update the model card and evaluation report so the displayed metrics match the data and code that generated them.
4. **Review location labels.** The forecast table includes labels such as `Indus River`, `Unknown`, neighborhoods, and other non-city place names. A dashboard must not label every model category as a supported city. Improve normalization or explicitly filter to the named supported cities.

### Match the hackathon workflow

- **Stages 1–4:** Source declarations, Bronze/Silver/Gold processing, quality gates, and model evaluation exist. Gold counts are extremely sparse in the inspected snapshot (142 rows), and the evaluation shows the weekly baseline beating Poisson.
- **Stage 5 (RAG):** Document chunks are embedded and searched with Chroma vector similarity. Gold counts and saved forecasts are retrieved by SQL and attached as structured evidence. The current retriever does **not** implement the guide's hybrid keyword-plus-vector search or a reranker.
- **Stage 6 (Generative layer):** OpenRouter answer generation, evidence thresholding, citations, and a fixed unsupported-answer response exist. Automated tests mock the API. There is no golden Q&A evaluation set, measured faithfulness/citation-accuracy report, prompt-version record, or human review process in the repository yet.
- **Stage 7 (Deployment):** GitHub Actions is the scheduled data/model/Supabase writer. A Streamlit dashboard and its Supabase read path are not implemented. There is no live dashboard, failure alert UI, or submitted demo artifact in this checkout.

### Operational details

- GitHub's runner is temporary. The workflow uses Actions cache to carry the warehouse forward; cache is not durable storage. Supabase is the durable Gold/forecast copy after a successful sync.
- GitHub Actions upserts by stable keys. A failed sync can leave some tables updated and others not; rerunning is safe because upserts merge on the same keys.
- The DuckDB forecast table trims old rows to a 30-day window. The current Supabase sync upserts but does not delete old remote forecasts, so Supabase can retain more than 30 days unless a cleanup step is added.
- The GitHub hourly workflow includes the Supabase sync. `run_hourly.ps1` on Windows currently runs data build and forecast only; it does not run the Supabase sync.
- RAG indexing is not an hourly workflow step. It fetches the selected pages and rebuilds Chroma when you run `rag-index`. The hourly job refreshes data and forecasts, not documents or embeddings.
- The tests use local fixtures and mocks. They verify code behavior, not current availability of news/weather websites, Hugging Face, OpenRouter, GitHub secrets, Supabase credentials, or Supabase table definitions.

## End-to-end run order on Windows PowerShell

Run commands from the project folder. These examples use the project's `.venv`; do not use Conda.

### 1. Create or activate the environment

For a new checkout:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,rag]"
```

`.[dev]` installs pytest and test dependencies. `.[rag]` adds Chroma, PDF parsing, and the embedding-model runtime. The first RAG use needs the embedding model available locally; the current audit machine could not fetch it because DNS to Hugging Face failed.

If the virtual environment already exists:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,rag]"
```

Expected check:

```powershell
python --version
python -m civicpulse --help
```

The first command should show the selected Python version; the second should print available CivicPulse commands.

### 2. Run the automated checks

```powershell
python -m pytest -q
```

Expected: a short summary like `58 passed`. If a test fails, fix that before ingesting or deploying. The GitHub hourly workflow also runs this test suite before starting its scheduled data job.

### 3. Inspect what is already stored

```powershell
python -m civicpulse stats
```

Expected sections: Bronze counts, Silver/quarantine counts, raw-volume gate, latest DQ summary, and feed freshness. For this audit snapshot the raw gate passed, but weather coverage was only Karachi. `stats` reports local DuckDB data; it does not query Supabase.

### 4. Fetch and build the data layers

```powershell
python -m civicpulse data
```

Expected sequence: if the historical GDELT completion marker is absent or stale, the project downloads the configured sampled history; then it fetches recent FreeNewsAPI and GDELT data, verifies Bronze files against the manifest, builds Silver and Gold, and runs the data-quality gates. A successful run prints `Build complete` with row counts and the run ID. On a clean machine this may take a long time and needs network access. It stops on failed source downloads, manifest/hash errors, or the configured raw-volume gate.

To rebuild only from files already present locally (still subject to the configured minimum-volume gate):

```powershell
python -m civicpulse build
```

### 5. Load historical weather if required

Weather ingestion is a separate command. It fetches one city and date range per command. Example:

```powershell
python -m civicpulse fetch-weather --city Karachi --latitude 24.8607 --longitude 67.0011 --start 2024-10-11 --end 2026-10-19
```

Expected: a saved JSON path under `data/bronze/open_meteo/`. Then rebuild:

```powershell
python -m civicpulse build
```

Expected: build output includes a nonzero `weather_rows` count when valid Open-Meteo files are present. Repeat the fetch for each supported location you intend to analyze. A DNS error such as `getaddrinfo failed` means no file was fetched; resolve network/DNS access and retry. The GitHub workflow currently has its own fixed weather-fetch list, which should be aligned with the data coverage you claim.

### 6. Evaluate the locked model

```powershell
python -m civicpulse evaluate-model
```

Expected: selected alpha, locked cutoff, test-row count, Poisson MAE, baseline MAE, and largest group errors. It can take around two minutes on the inspected checkout. Compare Poisson with the weekly seasonal baseline; do not claim a model gain if the baseline is better.

### 7. Create current future forecasts

```powershell
python -m civicpulse forecast-model
```

Expected: forecast run ID, generation time, training cutoff, training row count, selected alpha, forecast row count, city/category count, and horizon. The command fits a rolling Poisson model on current Gold data, requests forecast weather, and writes the next 24 forecast hours to `gold.poisson_forecasts`. If forecast weather is unavailable, missing weather features are imputed; the forecasts remain estimates of future news-report counts, not confirmed incidents.

Then check the warehouse:

```powershell
python -m civicpulse stats
```

`stats` does not print forecast rows; it confirms the warehouse and DQ summary. The forecast command's run ID and row count are the direct confirmation that a forecast run was written.

### 8. Build or refresh the RAG document index

```powershell
python -m civicpulse rag-index
```

Expected: `RAG index complete` with chunk, local PDF, and website-page counts, followed by warnings for any unavailable or disallowed site. It reads local PDFs and project documents, fetches only configured pages, creates local embeddings, and persists the Chroma index under `data/rag/`. It requires the RAG extra, model availability, and (for web refresh/model download) network access. Re-run when reference documents change; this is not part of the hourly workflow.

### 9. Retrieve evidence without an LLM answer

```powershell
python -m civicpulse rag-retrieve --question "What did the Poisson model forecast for Water in Karachi?"
```

Expected: retrieved reference chunks and structured Gold/forecast evidence with row IDs and UTC hours. This command returns context only and does not call OpenRouter. It still needs the embedding model locally to vectorize the question; the audit attempt failed at Hugging Face DNS resolution.

### 10. Generate a grounded answer

Set `OPENROUTER_API_KEY` in the ignored local `.env` file using the key from your OpenRouter account. Do not put it in Git. Then run:

```powershell
python -m civicpulse rag-answer --question "What did the Poisson model forecast for Water in Karachi?"
```

Expected: a concise answer with citations and a Sources list, or the configured message `I currently don't have this knowledge. Please refer to the internet.` when retrieved evidence is insufficient. This calls OpenRouter and requires network access and a valid key. The test suite mocks this call and does not spend API credits.

### 11. Copy Gold and forecasts to Supabase

Create the tables and stable primary keys in Supabase first. Put `SUPABASE_URL` and a server-side `SUPABASE_SECRET_KEY` in local `.env` only if doing a manual local sync. Then run:

```powershell
python scripts\sync_gold_to_supabase.py
```

Expected: progress for each available table and `Supabase Gold sync complete: ... rows upserted.` Rows are sent in batches of 500 by default. The sync is not a read path; it only writes DuckDB Gold/forecasts to Supabase. Do not share the Secret key or put it in a browser app.

## GitHub Actions hourly run

Before enabling it:

1. Commit/push the workflow and code to the default branch.
2. Create the matching Supabase tables/primary keys.
3. Add GitHub repository Actions secrets named `SUPABASE_URL` and `SUPABASE_SECRET_KEY`.
4. In **Actions**, run **CivicPulse Hourly Pipeline** manually once.

Expected order in the run: install dependencies → `pytest -q` → restore cached warehouse state → fetch missing configured historical weather files → collect/build Gold → locked model evaluation → rolling Poisson forecasts → Supabase upsert → save cache. A successful run shows the pytest summary and `Supabase Gold sync complete`. A missing secret, missing Supabase table/key, or rejected API request fails the sync step. The watchdog can dispatch recovery runs but does not guarantee an exact schedule.

## Streamlit decision and safe access

Streamlit is the next UI step only after deciding the hosted data interfaces:

- Streamlit should read Gold counts and forecasts from Supabase, not from the GitHub runner's DuckDB.
- Give Streamlit a Supabase Publishable key and read-only RLS policies for the specific public tables. Keep the Supabase Secret key for GitHub's server-side writer only.
- Store local Streamlit settings in ignored `.streamlit/secrets.toml`; put deployed settings in Streamlit Community Cloud **App settings → Secrets**.
- Decide where the RAG vector index will live. Current Chroma is local under `data/rag/`, which is not a durable shared service for an ephemeral deployment. Also decide whether to add the guide's hybrid retrieval/reranking and golden-set answer evaluation before presenting RAG as complete.

## Before claiming the hackathon workflow is complete

- Reconcile model-evaluation documents with the current frozen dataset.
- Align actual weather files, Gold coverage, and declared city coverage.
- Review noisy/non-city labels and show observed reports as reports, not confirmed incidents.
- Add persistent Supabase read access for the dashboard and a durable RAG vector store.
- Decide whether to implement hybrid retrieval and reranking from the guide.
- Create a golden Q&A evaluation set and report answer faithfulness, citation correctness, and refusal behavior; keep human review for generated briefs.
- Add a cleanup policy for old Supabase forecast runs if 30-day retention is required.
- Complete the dashboard, monitoring/alerts, and submission artifacts after these data and evaluation decisions.

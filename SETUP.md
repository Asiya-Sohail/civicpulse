# Setup in VS Code (free tools only)

## 1. Install once
| Tool | Get it | Check |
|---|---|---|
| Python 3.10+ (3.11/3.12 best) | python.org (tick **Add to PATH** on Windows) | `python --version` |
| Git | git-scm.com | `git --version` |
| VS Code | code.visualstudio.com | |
| make (optional) | macOS/Linux: built in. Windows: `winget install ezwinports.make`, or use Git Bash / WSL | `make --version` |

No `make` on Windows? Every `make X` below has a plain equivalent shown in the Makefile
(e.g. `make data` = `python -m civicpulse data`).

## 2. VS Code extensions
Install: **Python** (Microsoft), **Pylance**, **Makefile Tools** (optional), and
**SQLTools + SQLTools DuckDB driver** (optional, to browse `data/warehouse/civicpulse.duckdb`).

## 3. Project environment
Open the `civicpulse` folder in VS Code (File > Open Folder), then open the terminal (Ctrl+`):

```bash
python -m venv .venv
# Windows PowerShell:   .venv\Scripts\Activate.ps1
# Windows cmd:          .venv\Scripts\activate.bat
# macOS / Linux:        source .venv/bin/activate
make setup            # or:  python -m pip install -e ".[dev]"
```
Then Ctrl+Shift+P > **Python: Select Interpreter** > pick `.venv`.
(PowerShell blocks scripts? run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.)

## 4. Git
```bash
git init && git add . && git commit -m "Initial source declaration + warehouse skeleton"
```
The live source is FreeNewsAPI.ai and does not require an API key. `.env.example` contains only the optional
lookback-window setting for hourly polling.

## 5. Run
```bash
make test        # run the test suite
make probe       # FreeNewsAPI connectivity and response shape
make freenewsapi # one live Pakistan fetch
make gdelt       # download the configured sampled GDELT history only
make backfill    # fetch historical GDELT and build all available raw data through Gold
make data        # clean data: historical backfill, current fetches, integrity checks, 60k gate, and full build
make stats       # row counts, volume rule check, DQ summary, freshness
```
Quick GDELT test first: `python -m civicpulse fetch-gdelt --per-day 1 --max-files 20`, then `make build`.
The normal default is four evenly spaced GDELT exports per day. `make data` uses a versioned marker
to avoid repeating the historical fetch on hourly runs; if GDELT downloads fail, the build does not
write that marker and a later run can resume.

## 6. Inspect results
In a terminal: `python -c "import duckdb; print(duckdb.connect('data/warehouse/civicpulse.duckdb').sql('select * from quality_log order by logged_at_utc desc limit 30'))"`
or connect SQLTools to the `.duckdb` file. Useful tables: `quality_log`, `silver.quarantine`,
`gold.issue_events`, `gold.hourly_issue_counts`, `gold.data_freshness`, `gold.dq_summary`.


## 7. Schedule hourly runs on Windows (Task Scheduler)

The project includes `run_hourly.ps1`. It runs the same incremental job as `make data`,
fetches FreeNewsAPI Pakistan articles and recent completed GDELT 15-minute slots, rebuilds the
warehouse, fits the rolling Poisson model, saves its next 24 hours of forecasts, and appends output to `logs/hourly-YYYY-MM-DD.log`. It does **not** download
the full historical GDELT window every hour.

1. Run one successful manual test first:
   ```powershell
   .\.venv\Scripts\python.exe -m civicpulse data
   .\.venv\Scripts\python.exe -m civicpulse forecast-model
   .\.venv\Scripts\python.exe -m civicpulse stats
   ```
2. Open **Task Scheduler** → **Create Task** (not “Create Basic Task”).
3. **General**: name it `CivicPulse hourly data`; choose your Windows account. For a laptop,
   choose “Run only when user is logged on” initially so errors are easy to inspect.
4. **Triggers** → **New** → Begin task “On a schedule” → Daily → “Repeat task every: 1 hour”
   → “for a duration of: 1 day” → Enabled.
5. **Actions** → **New** → Action “Start a program”.
   - Program/script: `powershell.exe`
   - Add arguments: `-NoProfile -ExecutionPolicy Bypass -File "C:\\FULL\\PATH\\TO\\civicpulse\\run_hourly.ps1"`
   - Replace the example with the actual absolute path to your project.
6. **Conditions**: uncheck “Start the task only if the computer is on AC power” if appropriate.
   **Settings**: enable “Run task as soon as possible after a scheduled start is missed”.
   Avoid enabling parallel instances; a new run should not overlap an unfinished run.
7. Right-click the task → **Run**. Check `logs/` and then run `python -m civicpulse stats`.

FreeNewsAPI is keyless and documents a 20 requests/second per-IP limit. The collector makes
sequential requests and uses a 4-hour overlapping crawl-time window to recover short scheduling
gaps; increase `FREENEWSAPI_LOOKBACK_HOURS` for a longer recovery window. Repeated article URLs
are deduplicated during the warehouse build. GDELT's historical backfill is a sampled set of
15-minute exports, not a complete archive.


## 8. Run hourly on GitHub Actions (recommended for cloud scheduling)

The repository includes two workflows:

- `.github/workflows/hourly-data.yml`: runs at minute 17 of every UTC hour, rebuilds Gold, evaluates the locked model, generates and saves next-24-hour Poisson forecasts, and can also be started with **Actions → CivicPulse Hourly Pipeline → Run workflow**.
- `.github/workflows/hourly-watchdog.yml`: checks every 15 minutes and dispatches a recovery run if the hourly workflow is overdue or has failed.

On the first runner without the `.gdelt-history-complete-v5` marker, the hourly workflow runs the
four-per-day sampled historical backfill and builds it through Bronze, Silver, and Gold before its normal
incremental update. That first run takes longer. It marks the backfill complete only after
downloads and the warehouse build succeed; subsequent runs restore that marker and only fetch
recent GDELT slots. Raw archives, the manifest, parquet parse cache, DuckDB warehouse, and logs
are restored and saved via separate cache restore/save actions.

### One-time GitHub setup

1. Push this project to a GitHub repository and make sure both files under `.github/workflows/` are committed to the **default branch**. Scheduled workflows run from the default branch.
2. In Supabase, run the Gold-table SQL so the REST API tables and primary keys exist before the first sync.
3. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret** and add `SUPABASE_URL` and `SUPABASE_SECRET_KEY`. Get both from the Supabase project dashboard under **Settings → API Keys**. Use a server-side Secret key (`sb_secret_...`), never a Publishable key, for the hourly writer.
4. Open **Settings → Actions → General → Workflow permissions**. The hourly workflow only needs read access; the watchdog declares `actions: write` in its own workflow so it can dispatch a recovery run. If your organization restricts workflow permissions, an administrator may need to permit it.
5. Open **Actions**, enable workflows if prompted, select **CivicPulse Hourly Pipeline**, and click **Run workflow** for the first test. The job runs `python -m pytest -q` first and stops if a test fails; after that it runs the pipeline and `scripts/sync_gold_to_supabase.py`. Check the test summary and the `Supabase Gold sync complete` message in the run logs.
6. Confirm that the watchdog workflow also runs. It checks the hourly run schedule and dispatches a recovery attempt when the latest run is overdue or failed.

The Supabase sync uses upserts: `issue_events` and `hourly_issue_counts` conflict on `row_id`; weather conflicts on `(city, hour_utc, manifest_id)`; forecasts conflict on `(forecast_run_id, forecast_for_utc, city, issue_type)`; freshness conflicts on `source`. Keep these primary/unique keys in the Supabase tables. The workflow runner is temporary; Supabase is the persistent copy the app can read.

### Streamlit deployment secrets and reading Supabase

The Streamlit app reads current Gold counts and saved forecasts from Supabase. The hourly workflow remains the writer; the app does not fetch or rebuild the news/weather warehouse.

For the Streamlit app, use a Publishable key plus read-only Row Level Security policies, rather than the powerful server Secret key used by the hourly writer. Keep this configuration server-side in Streamlit secrets. For local development, create the ignored file `.streamlit/secrets.toml`:

```toml
SUPABASE_URL = "https://your-project-ref.supabase.co"
SUPABASE_PUBLISHABLE_KEY = "sb_publishable_..."
```

On Streamlit Community Cloud, open the app's **Settings → Secrets** and paste the same TOML entries there, adding `OPENROUTER_API_KEY` for cited answers. Do not commit `secrets.toml`. Enable RLS on the tables and grant the app's Publishable-key role only the `SELECT` access it needs; the GitHub workflow's Secret key can still write and bypass RLS. See Supabase's [Row Level Security guide](https://supabase.com/docs/guides/database/postgres/row-level-security) and Streamlit's [secrets management guide](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management).

### Schedule and reliability notes

- Cron expressions use **UTC**. `17 * * * *` means minute 17 of every hour (for example, 12:17 UTC = 17:17 Pakistan Standard Time).
- GitHub Actions scheduled events are best-effort: GitHub can delay or drop a scheduled start during heavy load, and no cron setup can guarantee execution at the exact second. The watchdog provides recovery, not a hard real-time SLA.
- GitHub-hosted runners are ephemeral. The workflow uses the Actions cache to restore and save `data/` (Bronze files, manifest, cache and DuckDB state) between runs. Cache is best-effort and subject to eviction/size limits; it is not durable database storage. If losing warehouse state is unacceptable, move raw files and the database to durable object storage or a managed database before production use.
- The hourly job searches FreeNewsAPI once or more per run depending on the number of result pages. Requests are sequential and stay well below the documented per-IP rate limit.
- Runs are serialized to avoid overlapping warehouse writes. Failed jobs retain logs as a 14-day artifact when log files exist.

## 10. Run and deploy the Streamlit app

The app reads `hourly_issue_counts` and `poisson_forecasts` from Supabase through its REST API. It does not read the deployment machine's DuckDB or try to fetch the news/weather feeds. The GitHub Actions hourly job remains the writer: it builds Gold and forecasts, then upserts those rows into Supabase. The dashboard caches reads for five minutes and has a manual refresh button.

The RAG document chunks and their embedding index are a separate deployment asset. This checkout includes the current Chroma index under `app_data/chroma` (about 9 MB); unlike the PDFs and raw warehouse under ignored `data/`, it will be included in Git. RAG searches that index, adds matching count/forecast evidence from Supabase, then sends it to OpenRouter. After changing PDFs or source documents, run `python -m civicpulse rag-index`, then replace `app_data/chroma` with the refreshed `data/rag/chroma` folder before pushing. Set `RAG_VECTORSTORE_DIR` in Streamlit secrets only if you store the index elsewhere. The embedding model also needs to be downloadable on the first app start or already cached by its host.

### Local run

1. Install the app and RAG packages:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -e ".[app,rag]"
   ```
2. Make sure the local document index exists; refresh it if the source PDFs or project reference documents changed:
   ```powershell
   .\.venv\Scripts\python.exe -m civicpulse rag-index
   ```
3. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill in the Supabase project URL, a Publishable key, and the OpenRouter API key. The real `secrets.toml` is ignored by Git.
4. In Supabase, enable RLS on `hourly_issue_counts` and `poisson_forecasts`, grant `SELECT` to the `anon` role, and add read policies for those two tables. For a public demo whose dashboard data is intended to be public, the policy expression can be `USING (true)`. Keep the GitHub writer's `SUPABASE_SECRET_KEY` only in GitHub Actions secrets; never use it in Streamlit.
5. Start the app:
   ```powershell
   .\.venv\Scripts\python.exe -m streamlit run app.py
   ```

### Streamlit Community Cloud

1. Push the application code and a deployable Chroma index to GitHub. Do not commit `.streamlit/secrets.toml`, `.env`, the Supabase Secret key, or the OpenRouter key. The app entry point is `app.py`; `requirements.txt` installs the app and RAG extras.
2. In Streamlit Community Cloud, create the app from the repository and select `app.py`.
3. In the app's **Settings → Secrets**, paste the values from `.streamlit/secrets.toml.example` with real credentials.
4. Confirm the Supabase read policies allow the `anon` role to select from `hourly_issue_counts` and `poisson_forecasts`.
5. Open the app. It should show recent rows returned from Supabase; the latest forecast run is selected from the forecast table. The app does not ingest new data itself. If no rows appear, check the hourly workflow's Supabase sync log and table/RLS setup.

The app uses only bounded reads (up to 500 count rows and 1,000 forecast rows) and refreshes its cache every five minutes. Its “Refresh data now” button clears that cache and reads Supabase again. The cited answer box needs the Chroma index and embedding model, Supabase read access for structured evidence, and OpenRouter for answer generation. If no supporting evidence is found, it uses CivicPulse's fixed “I currently don't have this knowledge” response.

## 9. Build and use RAG with the OpenRouter answer layer

### What the system does

1. Reads `data/pdf/NDMA.pdf`, `data/pdf/PCP.pdf`, and CivicPulse model/data documentation.
2. Fetches at most one page from each configured website: PMD, KW&SC, and K-Electric. It checks `robots.txt` and does not crawl links. Citizen Portal guidance comes from `PCP.pdf`.
3. Archives fetched pages and PDF copies under `data/bronze/rag_sources` and records provenance in `data/manifest.csv`.
4. Extracts text, splits it into chunks, and attaches source metadata such as title, publisher, URL, date, location, and chunk ID.
5. Runs a multilingual embedding model locally and saves the searchable vectors and metadata in Chroma under `data/rag/`.
6. For a question, searches Chroma for relevant document chunks.
7. For a question naming a recognized city or issue, reads matching **observed report counts** directly from `gold.hourly_issue_counts`. These are news-report counts, not confirmed incidents.
8. For a question naming both a recognized city and issue, retrieves that city's saved 24-hour Poisson forecasts and 95% intervals from `gold.poisson_forecasts`. Forecasts are produced by the hourly job and carry their generation time, training cutoff, and run ID.
9. `rag-retrieve` displays the retrieved context only. `rag-answer` sends that context and the question to OpenRouter, which is instructed to answer from the supplied evidence, cite sources, and use the fallback message if the evidence is insufficient.

### Setup and commands (PowerShell)

Install the optional RAG packages once:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[rag]"
```

To refresh the warehouse from its data sources, run:

```powershell
.\.venv\Scripts\python.exe -m civicpulse data
```

This updates Gold data used for observed counts. The hourly workflow then fits the rolling model and saves its next 24 hours of forecasts to `gold.poisson_forecasts`. Gold rows and forecasts are read directly at query time; they are not stored in the document vector index.

To create a forecast immediately outside the scheduled workflow, run:

```powershell
.\.venv\Scripts\python.exe -m civicpulse forecast-model
```

It uses the latest Gold observations, requests hourly forecast weather for supported cities, saves the next 24 hours into DuckDB, and keeps forecast runs for 30 days. Run `civicpulse data` first so the forecast uses the latest warehouse.

Build or refresh the document index:

```powershell
.\.venv\Scripts\python.exe -m civicpulse rag-index
```

The first index build downloads the embedding model. Later builds reuse its local cache. Re-run `rag-index` after changing the PDFs or reference documents; `rag-answer` does not refresh website/document content automatically.

To inspect retrieved evidence without generating an answer:

```powershell
.\.venv\Scripts\python.exe -m civicpulse rag-retrieve --question "How can a resident report a water supply problem?"
```

To generate an answer with citations, copy the OpenRouter settings from `.env.example` to your private `.env` file and set `OPENROUTER_API_KEY`. Keep the key private. Then run:

```powershell
.\.venv\Scripts\python.exe -m civicpulse rag-answer --question "How can a resident report a water supply problem?"
```

For a question about model results, include both a known city and issue, for example:

```powershell
.\.venv\Scripts\python.exe -m civicpulse rag-answer --question "What did the Poisson model predict for water reports in Karachi?"
```

The answer includes source titles/publishers/URLs or stable Gold/model result IDs. If evidence is insufficient, it returns: `I currently don't have this knowledge. Please refer to the internet.` The hourly workflow updates Gold and forecasts but does not rebuild document embeddings or run OpenRouter; RAG reads the newest Gold/forecast rows when a question is asked. Re-index documents only when reference content changes.

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
2. Open **Settings → Actions → General → Workflow permissions**. The hourly workflow only needs read access; the watchdog declares `actions: write` in its own workflow so it can dispatch a recovery run. If your organization restricts workflow permissions, an administrator may need to permit it.
3. Open **Actions**, enable workflows if prompted, select **CivicPulse Hourly Pipeline**, and click **Run workflow** for the first test. Then check the run logs and `python -m pytest -q` results shown in the Actions job.
4. Confirm that the watchdog workflow also runs. It checks the hourly run schedule and dispatches a recovery attempt when the latest run is overdue or failed.

### Schedule and reliability notes

- Cron expressions use **UTC**. `17 * * * *` means minute 17 of every hour (for example, 12:17 UTC = 17:17 Pakistan Standard Time).
- GitHub Actions scheduled events are best-effort: GitHub can delay or drop a scheduled start during heavy load, and no cron setup can guarantee execution at the exact second. The watchdog provides recovery, not a hard real-time SLA.
- GitHub-hosted runners are ephemeral. The workflow uses the Actions cache to restore and save `data/` (Bronze files, manifest, cache and DuckDB state) between runs. Cache is best-effort and subject to eviction/size limits; it is not durable database storage. If losing warehouse state is unacceptable, move raw files and the database to durable object storage or a managed database before production use.
- The hourly job searches FreeNewsAPI once or more per run depending on the number of result pages. Requests are sequential and stay well below the documented per-IP rate limit.
- Runs are serialized to avoid overlapping warehouse writes. Failed jobs retain logs as a 14-day artifact when log files exist.

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

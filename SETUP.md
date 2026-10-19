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

## 4. Git + key
```bash
git init && git add . && git commit -m "Initial source declaration + warehouse skeleton"
cp .env.example .env     # Windows: copy .env.example .env
```
Sign up for a free key at newsagentdata.com, then fill `.env`:
`NEWSAGENT_API_KEY` only. The other NewsAgent settings are pre-filled and verified against their OpenAPI spec
(`NEWSAGENT_BASE_URL` must be `https://api.newsagentdata.com` with no `/v1`).
`.env` is git-ignored; never commit it.

## 5. Run
```bash
make test        # 10 tests should pass
make probe       # NewsAgent: does country=pk return Pakistani news? are field names right?
make newsagent   # one live pull
make gdelt       # one-time historical GDELT backfill (sampled; first run is slow)
make data        # incremental recent fetch + rebuild bronze/silver/gold + DQ gate
make stats       # row counts, volume rule check, DQ summary, freshness
```
Quick GDELT test first: `python -m civicpulse fetch-gdelt --per-day 1 --max-files 20`, then `make build`.

## 6. Inspect results
In a terminal: `python -c "import duckdb; print(duckdb.connect('data/warehouse/civicpulse.duckdb').sql('select * from quality_log order by logged_at_utc desc limit 30'))"`
or connect SQLTools to the `.duckdb` file. Useful tables: `quality_log`, `silver.quarantine`,
`gold.issue_events`, `gold.hourly_issue_counts`, `gold.data_freshness`, `gold.dq_summary`.


## 7. Schedule hourly runs on Windows (Task Scheduler)

The project includes `run_hourly.ps1`. It runs the same incremental job as `make data`,
fetches NewsAgent once per run and recent completed GDELT 15-minute slots, rebuilds the
warehouse, and appends output to `logs/hourly-YYYY-MM-DD.log`. It does **not** download
the full historical GDELT window every hour.

1. Run one successful manual test first:
   ```powershell
   .\.venv\Scripts\python.exe -m civicpulse data
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

**Important:** the NewsAgent free tier shown by this project is 100 requests/day. Hourly polling is
about 24 requests/day, but avoid running `make data` repeatedly while debugging. The provider's
`fetched_at` is not necessarily the article's original publication time; the current field mapping
uses it as a fallback, so freshness is feed-fetch freshness, not guaranteed publication freshness.
GDELT's historical backfill is a sampled set of 15-minute exports, not a complete archive.


## 8. Run hourly on GitHub Actions (recommended for cloud scheduling)

The repository includes two workflows:

- `.github/workflows/hourly-data.yml`: runs at minute 17 of every UTC hour, and can also be started with **Actions → CivicPulse hourly data → Run workflow**.
- `.github/workflows/hourly-watchdog.yml`: checks every 15 minutes and dispatches a recovery run if the hourly workflow is overdue or has failed.

### One-time GitHub setup

1. Push this project to a GitHub repository and make sure both files under `.github/workflows/` are committed to the **default branch**. Scheduled workflows run from the default branch.
2. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret**.
3. Add `NEWSAGENT_API_KEY` with your NewsAgent API key. Do not put the key in source code or commit `.env`.
4. Open **Settings → Actions → General → Workflow permissions**. The hourly workflow only needs read access; the watchdog declares `actions: write` in its own workflow so it can dispatch a recovery run. If your organization restricts workflow permissions, an administrator may need to permit it.
5. Open **Actions**, enable workflows if prompted, select **CivicPulse hourly data**, and click **Run workflow** for the first test. Then check the run logs and `python -m pytest -q` results shown in the Actions job.
6. Confirm that the watchdog workflow also runs. It checks the hourly run schedule and dispatches a recovery attempt when the latest run is overdue or failed.

### Schedule and reliability notes

- Cron expressions use **UTC**. `17 * * * *` means minute 17 of every hour (for example, 12:17 UTC = 17:17 Pakistan Standard Time).
- GitHub Actions scheduled events are best-effort: GitHub can delay or drop a scheduled start during heavy load, and no cron setup can guarantee execution at the exact second. The watchdog provides recovery, not a hard real-time SLA.
- GitHub-hosted runners are ephemeral. The workflow uses the Actions cache to restore and save `data/` (Bronze files, manifest, cache and DuckDB state) between runs. Cache is best-effort and subject to eviction/size limits; it is not durable database storage. If losing warehouse state is unacceptable, move raw files and the database to durable object storage or a managed database before production use.
- The hourly job calls NewsAgent once per run. A free tier of 100 requests/day permits around 24 scheduled hourly calls, but manual runs and watchdog retries also consume quota.
- Runs are serialized to avoid overlapping warehouse writes. Failed jobs retain logs as a 14-day artifact when log files exist.

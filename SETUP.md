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
`NEWSAGENT_API_KEY`, `NEWSAGENT_BASE_URL`, and check the auth header name in their docs.
`.env` is git-ignored; never commit it.

## 5. Run
```bash
make test        # 10 tests should pass
make probe       # NewsAgent: does country=pk return Pakistani news? are field names right?
make newsagent   # one live pull
make gdelt       # history download, resumable (first run is slow; Ctrl+C is safe)
make data        # everything: fetch both + rebuild bronze/silver/gold + DQ gate
make stats       # row counts, volume rule check, DQ summary, freshness
```
Quick GDELT test first: `python -m civicpulse fetch-gdelt --per-day 1 --max-files 20`, then `make build`.

## 6. Inspect results
In a terminal: `python -c "import duckdb; print(duckdb.connect('data/warehouse/civicpulse.duckdb').sql('select * from quality_log order by logged_at_utc desc limit 30'))"`
or connect SQLTools to the `.duckdb` file. Useful tables: `quality_log`, `silver.quarantine`,
`gold.issue_events`, `gold.hourly_issue_counts`, `gold.data_freshness`, `gold.dq_summary`.

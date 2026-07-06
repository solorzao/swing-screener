# Desktop cockpit (Phase 1)

The native desktop app over the screener database: a pywebview window (or browser
tab) served by a local FastAPI backend, replacing the Streamlit dashboard screen by
screen. Design + phased plan: `docs/plans/2026-07-05-desktop-ui-design.md` and
`docs/plans/2026-07-05-desktop-ui-phase1.md`.

## Quickstart

```powershell
# 1. Install (once) — the cockpit extra brings FastAPI, uvicorn and pywebview
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,cockpit]"

# 2. Create the Start-menu shortcut (once; idempotent — rerun after moving the repo)
.\scripts\make_cockpit_shortcut.ps1

# 3. Double-click "Swing Screener Cockpit" in the Start menu
```

The shortcut runs `pythonw -m swing_screener.cockpit` with the repo root as working
directory, so the default `sqlite:///local.db` is the repo's `local.db`. No console
window opens; if startup fails, the launcher shows an error message box instead of
failing silently.

Without the shortcut (or without pywebview installed), the same thing in a browser
tab:

```powershell
.\.venv\Scripts\python -m swing_screener.cockpit --browser
```

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `SWING_DB_URL` (env) | `sqlite:///local.db` | database to point at — same variable the pipeline and dashboard read, so a configured box lands on its real data |
| `--db <url>` | `$SWING_DB_URL` | explicit override; wins over the env var |
| `--port <n>` | a free port | fixed port (useful for the dev proxy, below) |
| `--browser` | off | open the default browser instead of a pywebview window |

One instance runs at a time: a second launch finds the live one (via a port-file +
health probe) and defers politely.

The Start-menu icon is the committed `src/swing_screener/cockpit/assets/cockpit.ico`
(regenerate with `scripts/make_cockpit_icon.py`, then re-run the shortcut script).

## Pointing at Azure (your real data)

The cockpit reads whatever database `SWING_DB_URL` names — the same variable the
dashboard and pipeline use. To run it over production Azure SQL:

```powershell
az login                       # DefaultAzureCredential picks this up
$env:SWING_DB_URL = "mssql+pyodbc://@<server>.database.windows.net/swing?driver=ODBC+Driver+18+for+SQL+Server"
.\.venv\Scripts\python -m swing_screener.cockpit --browser
```

(Same URL form as the dashboard's Azure mode — see `docs/dashboard.md`; your client
IP must be allowed on the SQL server firewall, and the ODBC driver is auto-detected.)
Against prod, the heartbeat rail reads the REAL cadence (digest sends, market
weather, screen runs) and COHORTS grades the real forward book. Everything is
read-only. The in-app Local ⇄ Azure switcher chip is Phase 2; today the env var /
`--db` flag is the switch — note the Start-menu shortcut runs without your shell's
session variables, so set `SWING_DB_URL` at the **user** level (System Properties →
Environment Variables) if you want the double-click to land on Azure by default.

## Troubleshooting: stale `local.db`

If the COHORTS panel shows **`database error (OperationalError)`** while the DB chip
is green, your `local.db` predates a schema change (the connectivity probe passes,
the query then hits a missing column). Bring the schema up to date from the repo
root:

```powershell
.\.venv\Scripts\python -m alembic upgrade head
```

Alembic reads `SWING_DB_URL` (default `sqlite:///local.db`) for the target database.
It ships in the `azure` extra, so if the import fails first install it:
`.\.venv\Scripts\python -m pip install alembic`. Running the pipeline also
self-migrates on startup.

## Dev loop (frontend work)

The built frontend is committed under `src/swing_screener/cockpit/static/`, so a
zero-Node clone runs as-is. To iterate on the UI with hot reload:

```powershell
# terminal 1 — backend on the port the Vite proxy expects
.\.venv\Scripts\python -m swing_screener.cockpit --browser --port 8901

# terminal 2 — Vite dev server, proxies /api to 8901
cd cockpit-ui
npm install
npm run dev
```

When done, `npm run build` regenerates `static/` — commit it in the same commit as
the source change so the packaged app never lags the source.

## What Phase 1 shows — and what it doesn't

Phase 1 is the shell: masthead (MASTER CAUTION lamp, DB chip, data-as-of clock),
the heartbeat rail (every scheduled job as Up/Late/Down/UNKNOWN), and the COHORTS
panel where every number is a full Stat — value, n, cluster count, CI band, and an
honest "not measured" tick where cost level isn't stamped yet. The Phase 2 controls
(cost selector, DISARM) are visible but deliberately dead. Phases 2–3 add settlement
cards, forward books, the funnel, playbooks/analyst/execution screens, the six
actions, and retire Streamlit pages as native equivalents land — the full sequencing
lives in the design doc (`docs/plans/2026-07-05-desktop-ui-design.md`, "Migration").

# Desktop cockpit

The native desktop app over the screener database: a pywebview window (or browser
tab) served by a local FastAPI backend, replacing the Streamlit dashboard screen by
screen. Design + phased plans: `docs/plans/2026-07-05-desktop-ui-design.md`,
`docs/plans/2026-07-05-desktop-ui-phase1.md` (shell, heartbeats, COHORTS) and
`docs/plans/2026-07-10-desktop-ui-phase2.md` (forward books, funnel, performance
parity, SSE).

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
| `SWING_GH_TOKEN` (env) | unset | GitHub token for the optional `GH ·` heartbeat rows (Actions read access is enough) |
| `SWING_GH_REPO` (env) | unset | `owner/repo` the workflow poller asks about; both GH vars are required — with either missing the GH rows read an honest UNKNOWN |

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
read-only. The in-app Local ⇄ Azure switcher chip has not shipped yet (Phase 3);
today the env var / `--db` flag is the switch — note the Start-menu shortcut runs without your shell's
session variables, so set `SWING_DB_URL` at the **user** level (System Properties →
Environment Variables) if you want the double-click to land on Azure by default.

When the Azure credential expires, the DB chip turns red and grows a **Sign in to
Azure** button — it runs `az login` (system browser) and the chip recovers on the
next health poll; nothing else to do. Terminal `az login` still works as the
fallback, and the button only exists in Azure mode (it can't fix a local file).

## Troubleshooting: expired Azure credential

If the DB chip is red with **`database unreachable (…)`** while pointed at Azure,
the cached credential has almost certainly expired. Click the chip's **Sign in to
Azure** button — or run `az login` in any terminal — and the chip goes green on the
next health poll.

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
the source change so the packaged app never lags the source (CI runs the frontend
lint + build and fails on any drift between `cockpit-ui/` and the committed
`static/`).

## API endpoints

All GET unless noted. Each route's docstring in `src/swing_screener/cockpit/api.py`
is the deep contract; this table is the map.

| Endpoint | What it answers |
|---|---|
| `/api/health` | connectivity + safe DB label; always 200 — `connected` carries the truth |
| `/api/heartbeats` | every scheduled job's pulse (up/late/down/unknown); the weekday jobs measure against a business-day calendar (weekends + NYSE holidays), plus three optional `GH ·` rows (below) |
| `/api/stats/cohorts?facet=` | play_type × strength cohort expectancies — every number a full Stat |
| `/api/stats/performance?play_type=&window=&facet=` | the retired Streamlit Screener Performance page as data: KPIs, variant leaderboard, arm deltas, breakdowns, equity curve |
| `/api/forward-books?facet=` | one settlement card per registered experiment, decision-forcing states first |
| `/api/funnel` | the latest daily digest's reversal funnel snapshot; `{"funnel": null}` before the first one |
| `/api/gate` | the advisory autonomy gate + today's analyst spend |
| `/api/events` | the SSE wake channel (below) |
| `POST /api/azure-login` | spawns `az login` to refresh the AAD credential (`X-Cockpit` header, Azure mode only) |

`facet` is `research` (default, the full paper book) or `gold`
(`would_surface`-truthy rows only — the would-have-hit-your-inbox slice; it is
deliberately thin until stamped history accrues, so THIN badges there are expected).

## Live updates (SSE)

`/api/events` emits a named `change` event whenever a server-side change token
moves. The token is seven max-watermarks (screen run, paper-trade insert, exit
event, sent email, market report, funnel snapshot, newest verdicts-file mtime)
re-read every 15s server-side — exit events are watched separately because a trade
close is an UPDATE to `paper_trades` that the trade-id watermark cannot see. One
event always fires on connect. The frontend treats every event purely as a refetch
trigger, and its 60s poll stays the floor: a dead SSE connection degrades silently
back to plain polling, never to a stale screen.

## GitHub Actions heartbeats (optional)

Set `SWING_GH_TOKEN` + `SWING_GH_REPO` (see Configuration) and the heartbeat rail's
three `GH ·` rows — optimizer, reflection, CI — poll the GitHub Actions API for each
workflow's last completed run. Both variables are read once at startup; with either
missing, or on any polling failure, the rows read UNKNOWN rather than a false green.

## The reversal funnel table

`/api/funnel` reads `reversal_funnels`, a table the daily digest writes one row into
per run. The stages (detected → confirmed → fresh → actionable → surfaced) are
digest-time state and cannot be reconstructed afterwards, so history accrues from
the ship date only. It is a new TABLE (not a column), so local sqlite auto-creates
it via `get_engine`'s `create_all`; Azure gets it from migration `d7e4b2f9a1c6` on
the pipeline's next self-migrating run. Until the first daily digest after deploy,
the endpoint returns `{"funnel": null}` and the cockpit shows the designed empty
state.

## The experiment registry (`edge/experiments.json`)

Every forward experiment (exit arm or screen variant) is pre-registered here — one
entry per roster name in `pipeline/arms.py` / `pipeline/variants.py`, and a lockstep
test (`tests/pipeline/test_registry.py`) enforces both directions, so a roster entry
without a registry row (or vice versa) fails CI, not the UI.

- **To register:** add the roster entry and the registry entry in one PR —
  hypothesis, the stopping rule verbatim (it renders on the settlement card exactly
  as written), `mde_r`, `target_ci_halfwidth_r`, and `registered_sha` = the sha of
  the commit that adds the roster line (practically: that PR's merge commit).
- **To retire:** settlement decisions are human PRs that edit the registry and the
  roster together — DELETE the roster line, KEEP the registry row with
  `status: "retired"` plus `decided_at` and `decision`. The audit record survives;
  the lockstep test matches active entries only.

## What the cockpit shows — and what it doesn't

Phases 1 + 2 are shipped: the masthead (MASTER CAUTION lamp, DB chip, data-as-of
clock, the research/gold facet toggle, the cost selector), the heartbeat rail
(business-day aware, optional GH rows), the COHORTS panel where every number is a
full Stat, the FORWARD BOOKS settlement cards with their verbatim stopping rules,
the reversal FunnelBar, the PERFORMANCE panel (Streamlit parity), and the SSE wake
channel. The Streamlit "Screener Performance" and "System Health" pages are retired
— the cockpit owns those surfaces now. Still deliberately dead or absent: DISARM and
the rest of the six actions, the `@0.10` cost level (disabled-honest — no re-priced
book exists at that level), and the Playbooks / Analyst / Execution / Market-Weather
screens — all Phase 3, per the design doc's Migration section
(`docs/plans/2026-07-05-desktop-ui-design.md`).

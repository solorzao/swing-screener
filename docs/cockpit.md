# Desktop cockpit

The native desktop app over the screener database: a pywebview window (or browser
tab) served by a local FastAPI backend. It is the project's only instrument panel —
it replaced the retired Streamlit dashboard entirely (Phase 3). Design + phased plans:
`docs/plans/2026-07-05-desktop-ui-design.md`,
`docs/plans/2026-07-05-desktop-ui-phase1.md` (shell, heartbeats, COHORTS),
`docs/plans/2026-07-10-desktop-ui-phase2.md` (forward books, funnel, performance
parity, SSE) and `docs/plans/2026-07-11-desktop-ui-phase3.md` (the remaining screens,
the six actions, the Streamlit retirement).

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

## The screens

The cockpit is **ten screens**. Nine carry a digit and are jumped to with the number
keys `1`–`9`; the tenth, Reference, has no digit and rides a **REFERENCE** link in the
masthead. `cockpit-ui/src/lib/screens.ts` is the one source of screen-naming truth —
the keydown map, the masthead current-screen indicator, and every placeholder title
render from it.

| Key | Screen | What it is |
|---|---|---|
| `1` | **Mission Control** | the home dashboard: the masthead lamps, heartbeat pulse, COHORTS, plus the Phase-3 zones — Zone B (open positions / risk strip), Zone D (today's surfaced picks), Zone E (the event ticker) |
| `2` | **Candidates** | today's picks in digest order (continuation + reversal), each graded live against its latest close (✅ actionable / 🏃 already ran / ⛔ stopped), with the flagged extras that never consume the top-5 slots, plus the reversal funnel |
| `3` | **Positions & Ledger** | open real + live-book positions with live P/L, bracket lamps and risk badges; the closed-trade ledger + equity curve; the **log-trade** and **close-trade** forms |
| `4` | **Forward Books** | the settlement wall — one card per registered experiment with its verbatim stopping rule, decision-forcing states first |
| `5` | **Playbooks** | each strategy's edge file rendered from its sidecar: verdict rows with tier chips, the drift lamp, reflection-due counter, and the proposal decisions block (**approve** / **withdraw**) |
| `6` | **Analyst** | the insight-engine calibration (per-grade mean R, all shadow-book), nudge attribution, the unfilled-fraction split, spend today/7d/30d, and the deep-analysis surface (**request analysis**, status list, report viewer) |
| `7` | **Execution Safety** | the preflight GO/NO-GO checklist, the three locks + caps mandate, the bracket-shield table, and the masthead **DISARM** control's screen home |
| `8` | **Market Weather** | the latest weekly macro report (HA alignment, VIX/yields/bonds) + its history; the deterministic-fallback badge |
| `9` | **Systems** | the heartbeat rail full-width + playbook-integrity (the registry lockstep view) |
| — | **Reference** | universe (+ sector), the digest/email log, and the filterable exit log (the three facets) — reached via the masthead **REFERENCE** link (screen 10) |

The masthead's current-screen indicator reads `<n> · <TITLE>` and clicking it (or
pressing `1`) returns to Mission Control. The `1`–`9` keydown is guarded by a
typing-context bail (INPUT/TEXTAREA/SELECT/contentEditable, and any modifier held), so
the digits never steal a keystroke from a form field. The `/` command palette, `j/k`
row motion and Enter drill-in are deliberately deferred (see below).

## The six actions

Everything else the cockpit does is read-only. Exactly **six endpoints mutate state**,
each guarded by the `X-Cockpit` header (a same-origin guard → 403 without it), each
returning a precondition 409 rather than a silent no-op, and each waking every other
open window via the post-action nonce on the SSE token.

| Action | Endpoint | Effect + guardrail |
|---|---|---|
| **Log trade** | `POST /api/trades` | opens a real Trade. Prefilled from a Signal via `/api/trade-defaults`, but never blocks Oliver from taking it his way — when the entry leaves the signal's zone or the stop/target move, a server-computed **`override`** summary is stamped (engine plan vs. what happened), so the disagreement is kept, not lost. `signal_id` links the prefill source; a manual (unlinked) row carries an "unlinked (manual)" tag and no override text. |
| **Close trade** | `POST /api/trades/{id}/close` | closes an open trade (409 if already closed) and writes an `ExitEvent(reason='manual_close', is_paper=False, tier="")` so the close gets the same audit trail, moves the SSE exit watermark, and feeds the Zone E ticker. **`manual_close` is excluded from the exit-alert query** — the hourly exit job never emails you an urgent alert for a close you just performed yourself. |
| **Request deep analysis** | `POST /api/analysis` | queues a single-ticker Opus report. In the cloud the on-demand worker drains it every 15 min; locally it waits for `python -m swing_screener.notify.ondemand` (the UI says so). The status list flags a request `stalled` after 30 min, mirroring the worker's requeue window. |
| **Approve proposal** | `POST /api/proposals/{play_type}/{name}/approve` | flips a queued variant proposal to `approved`, appends a dated rationale, and raises a Needs-Your-Hand item carrying the verbatim **three-artifact promotion checklist** (variants.py roster line, experiments.json registry row, proposed.json flip — one commit). **Approve marks; it never promotes.** Promotion stays a deliberate human commit; the edit the cockpit writes is a local working-tree edit under `edge/`, committed with your decision. |
| **Withdraw proposal** | `POST /api/proposals/{play_type}/{name}/withdraw` | flips a queued/approved proposal to `withdrawn` with a dated reason. A withdrawn (or approved) name is not silently re-queued by the next reflection redraft — decided rows win the name collision. |
| **DISARM** | `POST /api/disarm?dry_run=0\|1` | the **venue sweep, and only that**: cancels resting entry-side orders and keeps/restores the bracket **stops** (restore levels are *copied* from ExecutionLog tickets, never computed). It never closes a position and never flips `SWING_EXECUTION_MODE` — the remote mode flip stays the Azure runbook's `az containerapp job update`. The button caption says exactly this: *"cancels entry-side, keeps bracket stops."* Hold-to-confirm (900 ms) fires the `dry_run=1` preview on hold-start and only arms the real POST once that preview has landed — a completed hold before the preview renders waits, it never fires blind. With no broker configured the endpoint 409s (`no broker configured`) and the masthead button is disabled with that tooltip. |

## The honesty posture

The cockpit is built so a false green can never appear. The invariants a reader (or a
reviewer) should hold it to:

- **UNKNOWN is never green — and never calm-safe.** A missing broker, an unreadable
  input, a `-inf` confidence bound, a failed venue read: all render a dashed UNKNOWN,
  distinct from both "ok" and "bad". A stale read is force-nulled to UNKNOWN rather
  than shown as yesterday's green (the Safety screen does this deliberately — a stale
  GO is worse than an honest UNKNOWN).
- **Approve marks; it never promotes.** No config write, no roster change, no
  real-money arming ever originates in the cockpit. The approve action produces a
  checklist and a working-tree edit; a human commits it (North Star #1/#3).
- **Every statistic is a full Stat with provenance.** Cohort/calibration numbers ride a
  10-key Stat (value, n, clusters, CI, bound type, cost level, corpus id, facet, unit);
  click any StatChip for the ProvenancePopover. Engine facts (price levels, counts,
  caps) ride plain because they are facts, not estimates.
- **The leak posture is class-names-only.** Error surfaces name the exception class,
  never its message payload.
- **Prices are last completed daily closes.** Every price surface is labelled *"as of
  last close"* — there are no intraday quotes (the engine is daily-close).
- **No saturated green exists yet, and that is correct.** The first green is a
  `forward_confirmed` TierChip on the Playbooks screen, and **zero such verdicts exist
  today** (continuation is all-hunch; reversal carries replay_screened rows that render
  amber). Playbooks ships amber-and-gray on purpose. Likewise the bracket lamps read
  UNKNOWN until a broker is configured (`SWING_BROKER` unset is today's reality).

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `SWING_DB_URL` (env) | `sqlite:///local.db` | database to point at — the same variable the pipeline reads, so a configured box lands on its real data |
| `--db <url>` | `$SWING_DB_URL` | explicit override; wins over the env var |
| `--port <n>` | a free port | fixed port (useful for the dev proxy, below) |
| `--browser` | off | open the default browser instead of a pywebview window |
| `SWING_GH_TOKEN` (env) | unset | GitHub token for the optional `GH ·` heartbeat rows (Actions read access is enough) |
| `SWING_GH_REPO` (env) | unset | `owner/repo` the workflow poller asks about; both GH vars are required — with either missing the GH rows read an honest UNKNOWN |
| `SWING_BROKER` (env) | unset | the venue for the bracket-shield lamps + DISARM. Unset (today's reality) → the lamps read UNKNOWN and DISARM is disabled; `broker_configured` on `/api/gate` and `/api/execution/safety` is this setting's truthiness, **not** connectivity |
| `SWING_EXECUTION_MODE` (env) | `off` | the execution adapter; the cockpit reads it for the safety screen's mode line. The cockpit never writes it — DISARM does not flip it |
| `SWING_BLOB_ACCOUNT_URL` / `SWING_BLOB_CONTAINER` (env) | unset | Azure Blob for the chart/PDF report proxies when pointed at Azure; locally the resolvers fall back to on-disk chart/PDF paths |

One instance runs at a time: a second launch finds the live one (via a port-file +
health probe) and defers politely.

The Start-menu icon is the committed `src/swing_screener/cockpit/assets/cockpit.ico`
(regenerate with `scripts/make_cockpit_icon.py`, then re-run the shortcut script).

## Pointing at Azure (your real data)

The cockpit reads whatever database `SWING_DB_URL` names — the same variable the
pipeline uses. To run it over production Azure SQL:

```powershell
az login                       # DefaultAzureCredential picks this up
$env:SWING_DB_URL = "mssql+pyodbc://@<server>.database.windows.net/swing?driver=ODBC+Driver+18+for+SQL+Server"
.\.venv\Scripts\python -m swing_screener.cockpit --browser
```

(See the [Azure deploy runbook](azure-deploy.md) for the connection-string forms; your
client IP must be allowed on the SQL server firewall, and the ODBC driver is auto-detected.)
Against prod, the heartbeat rail reads the REAL cadence (digest sends, market
weather, screen runs) and COHORTS grades the real forward book. The reads are
read-only; the six actions (above) write only what you invoke by hand. The in-app
Local ⇄ Azure switcher chip has not shipped yet (Phase 3); today the env var / `--db`
flag is the switch — note the Start-menu shortcut runs without your shell's session
variables, so set `SWING_DB_URL` at the **user** level (System Properties →
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

## Troubleshooting: stale `local.db` (and the Phase-3 migration trap)

If a data panel shows **`database error (OperationalError)`** while the DB chip is
green, your `local.db` predates a schema change (the connectivity probe passes, the
query then hits a missing column — after Phase 3 the usual culprit is the new
`trades.override` column).

The obvious fix — `alembic upgrade head` — **fails on a `local.db` that was born from
`create_all`** (the default local path). Such a file has all the tables but **no
`alembic_version` stamp**, so Alembic assumes an empty database and replays the
*initial* migration, which then dies with `table email_log already exists`. The
working recovery is to stamp the DB at the pre-Phase-3 head first, then upgrade:

```powershell
# alembic ships in the [azure] extra — install it first if the import fails
.\.venv\Scripts\python -m pip install -e ".[azure]"

# tell Alembic the schema is already at the pre-Phase-3 head (reversal_funnels),
# then apply only the Phase-3 migration (trades.override) forward
.\.venv\Scripts\python -m alembic stamp d7e4b2f9a1c6
.\.venv\Scripts\python -m alembic upgrade head
```

`d7e4b2f9a1c6` is the migration immediately before Phase 3 (`create reversal_funnels`);
the Phase-3 migration `e4b8a2d6f1c9` (`add override stamp to trades`) is the only one
`upgrade head` then applies. Alembic reads `SWING_DB_URL` (default
`sqlite:///local.db`) for the target database.

If you only need the one column and would rather not touch Alembic at all, the
equivalent one-liner is safe on local sqlite:

```sql
ALTER TABLE trades ADD COLUMN override VARCHAR(256);
```

(A **fresh** Azure SQL database never hits this — Alembic builds it from empty with a
proper `alembic_version` stamp, so `alembic upgrade head` there is correct and is what
the pipeline runs on startup.)

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

All GET unless noted. Each route's docstring in
`src/swing_screener/cockpit/routers/` is the deep contract; this table is the map. The
`X-Cockpit`-guarded POSTs are the six actions (plus the pre-existing `azure-login`).

**Reads — books & dashboards**

| Endpoint | What it answers |
|---|---|
| `/api/health` | connectivity + safe DB label; always 200 — `connected` carries the truth |
| `/api/heartbeats` | every scheduled job's pulse (up/late/down/unknown); weekday jobs measure against a business-day calendar, plus three optional `GH ·` rows |
| `/api/stats/cohorts?facet=` | play_type × strength cohort expectancies — every number a full Stat |
| `/api/stats/performance?play_type=&window=&facet=` | the retired Streamlit Screener Performance page as data: KPIs, variant leaderboard, arm deltas, breakdowns, equity curve |
| `/api/forward-books?facet=` | one settlement card per registered experiment, decision-forcing states first |
| `/api/funnel` | the latest daily digest's reversal funnel snapshot; `{"funnel": null}` before the first one |
| `/api/gate` | the advisory autonomy gate + today's analyst spend + `broker_configured` (for the masthead DISARM enablement) |

**Reads — Phase-3 screens**

| Endpoint | What it answers |
|---|---|
| `/api/picks` | today's surfaced picks in **digest order** (the five always match the email) + the liveness-dropped picks as flagged extras that never consume cap slots |
| `/api/positions` | open real Trades + open live PaperTrades with per-row P/L (per-row degradation — one bad row never 503s the zone), bracket lamps, the three cap gauges, closed trades + equity points, `quotes_as_of` |
| `/api/trade-defaults?signal_id=` | log-trade prefill: signal levels, actionability at the cached quote, `size_order` (shares==0 → "sizing unconfigured"), suggested entry clamped to the zone |
| `/api/analysis?limit=` | the deep-analysis queue: queued/running/done/failed + `stalled` flag + `worker` mode copy |
| `/api/analysis/{id}/chart/{index}`, `/api/analysis/{id}/pdf`, `/api/signals/{id}/chart` | server-resolved report assets (bytes, proper media types; 404 on aged-out blob / missing file / bad index — most signals have no chart, 404 is normal). Never client paths — arbitrary-read hole closed |
| `/api/proposals` | both play types' variant proposals + gate verdict + delta-vs-incumbent + noop flag |
| `/api/execution/safety` | `broker_configured`, the preflight report (None-broker branch, never a 500 on the default local setup), the three locks + caps mandate, mode + `env_scope`, the bracket-shield table (UNKNOWN-never-green) |
| `/api/playbooks` | per play type: md verbatim, frontmatter, verdict rows (with cost/corpus), the drift report, reflection-due, the falsified section |
| `/api/weather` | the latest MarketReport + history; `{"weather": null}` on empty |
| `/api/analyst` | insight-engine calibration (with zero-count grades), nudge attribution, the unfilled-fraction three-way split, spend today/7d/30d, all R labelled shadow-book |
| `/api/attention` | the permanent-poll strip feed: queued/approved-pending proposals, reflection-due play types, latest analysis id |
| `/api/ticker?limit=` | the Zone E event ticker — merged reverse-chron ExitEvent + ExecutionLog + EmailLog + AnalystCall + AnalysisRequest |
| `/api/exits?reason=&book=&account=&limit=` | the Reference exit log, three facets filterable (Book=is_paper and Account are different axes) |
| `/api/universe?search=`, `/api/emails?limit=` | the Reference universe (+ sector) and digest/email log |
| `/api/events` | the SSE wake channel (below) |

**Writes — the six actions + credential refresh** (all `X-Cockpit`)

| Endpoint | Action |
|---|---|
| `POST /api/trades` | log trade |
| `POST /api/trades/{id}/close` | close trade |
| `POST /api/analysis` | request deep analysis |
| `POST /api/proposals/{play_type}/{name}/approve` | approve proposal (marks, never promotes) |
| `POST /api/proposals/{play_type}/{name}/withdraw` | withdraw proposal |
| `POST /api/disarm?dry_run=0\|1` | DISARM (the venue sweep) |
| `POST /api/azure-login` | spawns `az login` to refresh the AAD credential (Azure mode only) |

`facet` is `research` (default, the full paper book) or `gold`
(`would_surface`-truthy rows only — the would-have-hit-your-inbox slice; it is
deliberately thin until stamped history accrues, so THIN badges there are expected).

## Live updates (SSE)

`/api/events` emits a named `change` event whenever a server-side change token moves.
The token is a set of cheap max-watermarks re-read every 15 s server-side, covering
**everything the six actions can touch**: the screen run, paper-trade + real-trade
inserts, the exit-event clock (a close is an UPDATE the trade-id watermark can't see,
so `max(ExitEvent.id)` is watched separately), sent emails, market reports, the funnel
snapshot, the analysis queue (id / finished_at / started_at / scored, so a claim and a
requeue both register), execution logs, analyst calls (id + scored count, since
scoring is an UPDATE), and the file mtimes of the verdict sidecars, proposal stores and
experiment registry. A lock-guarded **action nonce** rides the same token — every
successful action POST bumps it, so one window's log/close/approve/DISARM wakes all the
others. One event always fires on connect. The frontend treats every event purely as a
refetch trigger, and its 60 s poll stays the floor: a dead SSE connection degrades
silently back to plain polling, never to a stale screen.

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

The cockpit's **approve/withdraw** actions write the *proposal* decision (a working-tree
edit under `edge/`); the promotion into this registry stays a deliberate human commit —
the approve response hands you the exact three-artifact checklist to make it.

## What the cockpit shows — and what it doesn't

All three phases are shipped. The cockpit owns the whole instrument panel: the masthead
(MASTER CAUTION lamp, DB chip, data-as-of clock, facet toggle, cost selector, DISARM,
the REFERENCE link), the NeedsHandStrip and the Zone E event ticker as permanent
chrome, the ten screens above, and the six actions. The Streamlit dashboard —
including its Screener Performance and System Health pages, the trade-log/close, the
Deep Analysis page, Candidates, and the Reference views — is **deleted**; the cockpit
is the only surface now.

Deliberately deferred (with reasons, in the Phase 3 plan's non-goals):

- **Tray icon + native notifications** — every useful tray behavior is a foreign-thread
  WinForms call (the 2026-07-06 deadlock class); a future spike must demonstrate
  `run_detached` + `webview.start` coexistence and GUI-thread-safe show/hide first.
- **PyInstaller single-file packaging** (stage 2) — design-optional; needs the
  `--add-data` bundling of `static/`, `assets/` and the universe seed.
- **The `/` command palette, `j/k` row motion, and Enter drill-in** — deferred together
  (drill-in depends on row focus, which none of the three has scaffolding for yet). Only
  `1`–`9` ships.
- **The in-app Local ⇄ Azure switcher chip** — today the env var / `--db` flag is the
  switch.
- **Auto-promotion of proposals or any config write from the cockpit** — a human commit
  always (North Star #1/#3).
- **Remote execution-mode flips from the UI** — env is per-process; the Azure runbook
  owns the remote mode flip.
- **The `@0.10` cost level** — disabled-honest; no re-priced book exists at that level.
- **SAS report URLs** — the private container + server-side proxies is the posture.
- **Intraday quotes** — the engine is daily-close; every price is "as of last close".

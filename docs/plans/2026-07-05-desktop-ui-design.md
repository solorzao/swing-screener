# Desktop UI ("Dark Cockpit") — validated design

> Replaces the Streamlit dashboard (`src/swing_screener/dashboard/app.py`) with a local
> Windows **desktop application**: a web frontend in a native window over a local FastAPI
> backend, launched by double-clicking an icon. Designed 2026-07-05 via a three-concept
> panel (flight-deck / scientific-ledger / trading-desk lenses) + judge synthesis;
> user-validated against an interactive mockup. Phase 1 plan:
> `docs/plans/2026-07-05-desktop-ui-phase1.md`.

## Why replace it

The Streamlit app is a filing cabinet, not an instrument panel: 12 pages behind a sidebar
radio, so "is everything okay?" requires a tour; startup is a ritual (env vars + a
`streamlit run` incantation + a browser tab + a terminal that must stay open); and the
system's most important facts — is the Sunday loop alive, are the forward books accruing
toward settlement, is the confirmed edge still confirmed, am I stop-protected — have no
single home (three of them are shown nowhere). It also under-implements North Star
principle 2: several numbers render bare, with no n / CI / cost level / corpus.

## Philosophy — the dark cockpit

Airbus dark-cockpit doctrine: **a healthy system is dim and silent; light means attention
required.** Every panel is a state machine (OK / LATE / BREACH / SETTLED-AWAITING-DECISION),
not a report. The operator's daily loop is one glance at Mission Control: if nothing is
lit, close the window; if something is lit, one click lands on the exact evidence behind
the lamp.

### The three mechanical rules (principles as code, not habits)

1. **Stat objects, not floats.** The API returns
   `{value, n, n_clusters, ci_low, ci_high, cost_level, corpus_id, facet}` for every
   statistic; the frontend has **no renderer for a bare number**. Principle 2 becomes
   unrepresentable to violate. Where cost/corpus isn't persisted yet, the chip renders a
   hollow "not measured" tick — honest about plumbing debt, never a guess.
2. **Tier-gated color + n-gated contrast.** Nothing renders saturated green unless
   `forward_confirmed`; `replay_screened` is amber and labeled; red is reserved for
   breaches/direction and always paired with a shape (colorblind rule). n-gating:
   n<5 → a gray badge instead of a value; 5–11 → 45% opacity + THIN chip; ≥12 → full
   contrast; IID-fallback bounds get a dotted underbar.
3. **Six actions only.** Log trade, close trade, request deep analysis, approve proposal,
   withdraw proposal, DISARM — all structured forms with engine-supplied defaults;
   destructive ones are hold-to-confirm (900 ms). There is deliberately **no per-pick
   take/skip adjudication UI** — picks show cohort evidence, and the only row action is
   "log trade" (prefilled, override-flagged). This is the anti-discretion-creep guard.

## Mission Control layout (landing screen, fixed ~1400px, dark)

- **MASTHEAD** (44 px, on every screen): MASTER CAUTION lamp (aggregates every red/amber
  below; dark when clear) · DB switcher chip (Local SQLite ⇄ Azure SQL, never prints the
  connection string) · data-as-of clock · autonomy-gate chip (`NOT READY · 0
  forward_confirmed · 0/20 scored` or `READY`) · **cost-level selector** (net @0.05 /
  @0.10 ATR — re-stamps every historical number app-wide) · GOLD/RESEARCH facet toggle
  (research renders hatched) · execution-mode chip · **DISARM** hold-to-confirm, captioned
  with its exact effect ("cancels entry-side, keeps bracket stops" — PR #98 semantics).
- **NEEDS-YOUR-HAND strip**: pending decisions ARE lamps — settled-awaiting-decision
  experiments, proposals awaiting approve/withdraw, unread deep analyses, reflection-due.
  The empty state names the next expected event ("evening screen ~18:05 ET") so silence
  reads as health, not staleness.
- **ZONE A — SYSTEMS** (left rail, 280 px): Healthchecks-style heartbeat wall, one row per
  loop (evening screen, daily/weekly/monthly digest, intraday exit, market weather, GH
  optimizer, GH reflection, deep-analysis worker) with tri-state Up/Late/Down lamps
  computed from explicit **Period + Grace** (shown on hover) — plus a fourth state,
  **UNKNOWN** (gray, dashed): a dead or unconfigured poller is never allowed to look
  green. Below: playbook integrity (verdicts.json age, md-vs-json drift lamp,
  reflection-due counter) and open loop-PR count.
- **ZONE B — RISK** (top band): open real positions as strips — R at risk,
  distance-to-stop bar, **BracketShield lamp** (venue-held stop armed vs DB-number-only =
  amber), plus three cap gauges (daily notional / daily loss / concurrent). Empty state:
  dim "FLAT — no exposure".
- **ZONE C — FORWARD BOOKS** (center, the **largest** zone — the answer to "a quiet
  cockpit hides slow edge decay"): one settlement card per active experiment.
  SettlementBar (n accrued vs n needed for the target CI width, Eppo-style), ETA,
  current delta as StatChip, tier chip, trailing-expectancy sparkline, and the
  **pre-registered stopping rule verbatim with its git commit hash** (the solo
  pre-registration-theater mitigation). Settled-awaiting-decision cards glow amber and
  appear in the Needs-Your-Hand strip. Futility rules test "upper bound < MDE", never
  "lower bound < 0".
- **ZONE D — TODAY** (right column): today's picks (max ~8) with actionability lamp
  (live-reclassified), entry/stop/target LevelRail, the **cohort StatChip** (the tier's
  measured edge — not the pick's score), and a **ConvictionChip** that never renders an
  analyst grade without that grade's own scored track record ("B · unproven (0 scored)").
  Beneath: the reversal **FunnelBar** (detected → confirmed → fresh → actionable →
  surfaced) with proportional bars, hatched drop segments, and overflow-ticker chips.
- **ZONE E — TICKER** (bottom, 2 rows): merged reverse-chron event feed (exits, execution
  log, emails, analyst calls, reflections).

Keyboard: `1–9` jump screens, `/` command palette, `j/k` rows, `Enter` drill-in.

## Screens (beyond Mission Control)

1 Mission Control · 2 Candidates · 3 Positions & Ledger · 4 Forward Books ·
5 Playbooks (the edge files as first-class objects; **falsified claims stay visible in
strikethrough with the falsifying n/CI** — the continuation history must remain legible) ·
6 Analyst (calibration progress, unfilled fraction, nudge attribution) · 7 Execution
Safety (locks, caps, bracket state, disarm) · 8 Market Weather (currently shown nowhere) ·
9 Systems · 10 Reference. Every number opens a **ProvenancePopover** (source
table/function, corpus id + date range, facet, cost level, bound type).

## Component inventory

`StatChip` (value + graded 50/80/95% CI underbar + n·clusters badge + cost glyph,
compact table mode) · `TierChip` · `HeartbeatRow` · `SettlementBar` (+ Statsig-style
dashed "peek wings" on any pre-settlement CI) · `FunnelBar` · `CapGauge` · `BracketLamp` ·
`HoldToConfirm` · `ProvenancePopover` · `FacetToggle` · `DeltaDiff` (proposed-variant
config deltas with the code gate's verdict inline) · `ConvictionChip` · `EventTickerRow`.

## Technology (researched 2026-07-05; sources in the design-session transcript)

- **Shell: pywebview + FastAPI, one Python process tree.** All alternatives
  (Tauri sidecar, Electron) end up PyInstaller-bundling the same ~200 MB Python payload
  (SQLAlchemy/pandas/pyodbc/azure-identity dominate) and only add a second toolchain and
  sidecar-orphan bugs. WebView2 ships evergreen on Windows 11. Tray + native
  notifications via `pystray`; single instance via a named mutex.
- **Startup, two stages.** Stage 1: a Start-menu shortcut to
  `venv\Scripts\pythonw.exe -m swing_screener.cockpit` — identical double-click UX, zero
  packaging/AV friction. Stage 2 (optional later): PyInstaller `--onedir` (never
  `--onefile`: slow temp-dir extraction + AV heuristics). SmartScreen keys off
  Mark-of-the-Web — a locally built exe never triggers it; signing buys nothing since 2024.
- **Azure switch:** `InteractiveBrowserCredential` + `TokenCachePersistenceOptions`
  (one-time system-browser login, silent refresh); `AzureCliCredential` continues to work
  when `az` is on PATH. The existing `SWING_DB_ACCESS_TOKEN` static-token path (PR #100)
  is untouched — it is CI-only.
- **Frontend: Vite + React + TypeScript.** Charts: TradingView lightweight-charts v5
  (candles + annotation primitives) and uPlot (sparklines/small multiples — canvas,
  dozens per page). Tables: TanStack Table (headless; uPlot renders inside cells). Push:
  SSE via `sse-starlette` (auto-reconnect; data cadence is minutes — polling is an
  acceptable fallback). Built `dist/` is committed so a fresh clone runs with zero Node;
  Node is needed only to change the UI.

## Migration — never a big bang

- **Phase 1**: shell + masthead + heartbeat rail + Stat contract + plain tables reusing
  existing `repo`/`performance` queries. Streamlit keeps running untouched.
- **Phase 2**: StatChip everywhere + SettlementCards/Forward Books + funnel; retire the
  Streamlit Performance/Health pages.
- **Phase 3**: Playbooks/Analyst/Execution-Safety/Market-Weather screens + the six
  actions; retire the remaining Streamlit pages, delete `dashboard/`.
- Backend obligations surfaced by the design (small, sequenced into phases): persist the
  `rev_funnel` tuple; stamp cost level + corpus id per aggregate row (until then the UI
  shows the hollow "not measured" tick); optional GH/Azure pollers (rows render UNKNOWN
  until a token is configured).

## Risks (named, accepted)

- A quiet cockpit can hide slow decay → Forward Books is the dominant zone, every card
  carries a trend sparkline, and operational green never implies edge-exists (the tier
  chip owns that claim).
- Desk-blotter-style pick centering invites discretion creep → six-actions constraint,
  no take/skip UI.
- Solo pre-registration is theater-prone → stopping rules render verbatim with commit
  hash + date.
- Two build systems in one repo (Python + Vite) → committed `dist/`, UI changes are the
  only thing that need Node; CI treats `cockpit-ui/` as an independent job.
- Phase-1 posture: stale panels dim (opacity + "showing last good data" line) rather
  than blank — data visibility over blankness; the masthead alone force-nulls to UNKNOWN.
- Phase-1 posture: lamp shape-redundancy covers UNKNOWN only (hollow/dashed vs filled);
  up/late/down are hue+brightness until full shape coding lands in Phase 2.

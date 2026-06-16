# Dashboard modernization — design

**Date:** 2026-06-16
**Status:** Approved (brainstorming)
**Scope:** Modernize the local Streamlit dashboard. No backend/data-model rewrite.

## Goal

Turn the screener dashboard into a clean, modern, single-user web app where every
piece of data in the store is reachable, every screen degrades gracefully instead
of throwing, and the day-to-day flow — review candidates → enter trade → track
P/L → close → review performance — feels effortless.

**Success criteria**

1. No raw stack trace ever reaches the screen.
2. Connects to Azure SQL history on this machine without manual ODBC-driver fiddling.
3. Every table in `models.py` has a home in the UI.

## Decisions (from brainstorming)

- **Stack:** stay in Streamlit; overhaul presentation only. No React/API rebuild.
- **Look & feel:** clean light / modern SaaS — left sidebar nav, card metrics, soft
  surfaces, single indigo accent.

## 1. Information architecture

Replace the flat 6-tab bar with a left **sidebar navigation** (`st.navigation` +
`st.Page`), grouped by workflow:

- **Overview** *(new home)* — KPI cards (open positions, total unrealized P/L,
  today's candidate count, screener win rate) + recent activity. Replaces the
  empty "No candidates" landing.
- **Signals** → Today's Candidates (continuation *and* reversal; play-type filter).
- **Trades** → Active Trades · Trade Entry · Closed Trades.
- **Analytics** → Screener Performance · Exit Log.
- **Reference** *(new)* → Universe (searchable) · Digest Log (`email_log`).
- Sidebar footer: a clean **connection-status chip** (e.g. "🟢 Azure SQL · swing"),
  not the raw connection string currently leaking there.

## 2. Visual system

- **Theme** (`.streamlit/config.toml` `[theme]`): light base, indigo accent
  (`#4F46E5`), soft grey secondary surfaces, clean sans font, rounded corners.
- **Targeted CSS**: hide the default Deploy button + hamburger for app-like chrome,
  tighten padding, style metric cards (subtle border + shadow), define semantic
  P/L colors (green `#16A34A` / red `#DC2626`) used consistently.
- **Charts → Plotly** (replacing bare `st.bar_chart`/`line_chart`): tooltips,
  consistent palette, proper axis formatting for the equity curve and breakdowns.

## 3. Per-view upgrades

- **Tables via `column_config`**: score as `ProgressColumn`; prices/P/L as formatted
  `NumberColumn` (`$`, `%`); distance-to-stop/target as inline bars; play-type /
  quality as colored badges. Candidates surfaces the currently-hidden fields:
  `play_type`, `strength`, `rsi`, `atr`, `oversold`, `rank`.
- **Charts**: each candidate's chart moves into a per-row expander (no raw image stack).
- **Active Trades gains a close action**: select a row → "Close trade"
  (date / price / reason) → calls existing `repo.close_trade`. Closes the
  enter-but-can't-close gap.
- **Trade Entry**: same fields, cleaner layout + inline validation; optional prefill
  from a candidate.
- **Overview**: the new KPI landing.

## 4. Robustness — "no errors"

- **Auto-detect ODBC driver**: in `db/session.py`, pick the highest installed
  `ODBC Driver NN for SQL Server` instead of hardcoding 18. Fixes today's `IM002`
  crash on this machine (only Driver 17 present) and keeps prod parity.
- **Friendly failures**: a top-level connection guard plus per-view `try/except`
  render a calm "Can't reach the database" card with collapsible details — never a
  stack trace.
- **Empty states**: one consistent, friendly empty message per view.
- **Tests**: keep the `_render_*` / `quotes.latest_closes` seams; add coverage for
  driver detection and the new Universe / Digest queries.

## Out of scope (YAGNI)

React rebuild, auth / multi-user, hosting, real-time quotes, writing back to Azure
beyond the existing trade entry.

## New data-access helpers required

- `repo.list_universe(session, search=None)` — for the Universe view.
- `repo.list_email_log(session)` — for the Digest Log view.
- (Active Trades close action reuses existing `repo.close_trade`.)

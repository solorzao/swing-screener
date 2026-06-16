# Dashboard follow-ups — design

**Date:** 2026-06-16
**Status:** Approved (brainstorming)
**Scope:** Two follow-ups from the dashboard-modernization review. Independent.

## Follow-up 1 — Cache the dashboard DB engine

**Problem:** `render()` in `dashboard/app.py` calls `get_engine(db_url)` then
`engine.connect()` on EVERY rerun (every interaction). Against Azure SQL this
rebuilds the engine/pool and re-acquires an AAD token each time — needless latency.

**Approach:** Build the engine in a `@st.cache_resource`-decorated function keyed by
`db_url`, so the engine + connection pool + token listener are constructed once per
session. The 🟢/🔴 connection-status health check (`engine.connect()`) stays, but now
checks out a pooled connection (cheap) instead of re-logging-in.

**Test safety:** `tests/dashboard/test_connection.py` monkeypatches
`swing_screener.db.session.get_engine` to raise. `@st.cache_resource` does not cache
exceptions, and each dashboard test uses a unique `tmp_path` SQLite URL, so the
DB-down path still surfaces. Add a cache-clear fixture if anything proves sticky.
Both `test_db_down_shows_friendly_card_and_red_chip` and
`test_healthy_db_shows_green_chip` must stay green.

## Follow-up 2 — Persist + enrich the universe

**Problem:** `run_screen` loads the universe from `universe_seed.csv`
(ticker, name, exchange) but never writes the `universe` table, so the dashboard
Universe view is always empty. The `Universe` model also has unpopulated
`market_cap` and `avg_dollar_volume` columns.

**Decision:** Persist the seed AND enrich with both metrics (fullest option).

**Data layer (`data/fetch.py`):**
- `fetch_market_cap(ticker, *, cache_dir, today, ...) -> float | None` — mirrors
  `fetch_bars`: per-(ticker, day) cache, retries with backoff+jitter, returns `None`
  on any failure (never raises). Source: yfinance `fast_info` (verify exact accessor
  against the installed version; degrade to `None`).
- `avg_dollar_volume(daily_frame, window=20) -> float | None` — pure; mean of
  close×volume over the last `window` daily bars; `None` if the frame is empty.

**Repo:**
- `sync_universe(session, entries)` — upsert each seed entry (ticker→name/exchange)
  and delete rows whose ticker is no longer in the seed. Mirrors the seed while
  PRESERVING existing `market_cap`/`avg_dollar_volume`.
- `set_universe_metrics(session, ticker, *, market_cap, avg_dollar_volume)` — update a
  row's metrics, skipping any value that is `None` (a failed fetch keeps the prior).

**Pipeline wiring (`run_screen`):**
- Phase 1 (up front, own commit): `sync_universe(load_universe(universe_path))` over
  the FULL seed (before any `max_tickers` truncation), so the view fills immediately
  even if enrichment fails.
- Phase 2 (inside the existing per-ticker loop, already isolated): for each
  successfully-fetched ticker, compute `avg_dollar_volume` from its daily bars and
  best-effort `fetch_market_cap`, then `set_universe_metrics`.

**Dashboard:** no change — the Universe view already reads `list_universe` and renders
`market_cap`/`avg_dollar_volume` as `$` NumberColumns; they simply populate.

**Error handling:** market-cap fetch is the only new failure surface — cached, retried,
isolated, and never blocks a screen run. Cold runs get slower (one extra call per
ticker); cached re-runs do not.

**Testing:** pure `avg_dollar_volume` unit tests; `fetch_market_cap` with mocked
yfinance (success + failure→None); `sync_universe` (upsert + delete-not-in-seed +
metric preservation) and `set_universe_metrics` (None-skips) repo tests; a `run_screen`
test (mocked fetches) asserting the `universe` table is populated with metrics.

## Out of scope (YAGNI)
Parallelizing market-cap fetches; historical market-cap; a dashboard refresh button.

# Swing Screener — Phase 3 (Local Dashboard) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task. Build each testable component test-first per superpowers:test-driven-development.

**Goal:** A private, local **Streamlit** dashboard over the Phase 2 SQLite store: browse the day's ranked candidates, log and track real trades with live unrealized P/L and exit alerts, and review the shadow book's screener-performance stats.

**Architecture:** Keep all logic in **pure, tested** modules (trade repo CRUD, unrealized-P/L math, performance analytics, a mockable quote helper); the Streamlit `app.py` is a thin rendering layer over them, smoke-tested headlessly with `streamlit.testing.v1.AppTest`. The dashboard reads the same SQLite DB the pipeline writes, binds to `127.0.0.1` only, and never needs the internet except to refresh quotes.

**Tech Stack:** Python 3.12, Streamlit, SQLAlchemy 2.0 (existing), pandas. Charts: Streamlit built-ins (`st.line_chart`/`st.bar_chart`/`st.dataframe`) + the annotated HA PNGs the pipeline already renders (`st.image`). No extra viz dependency.

**Design reference:** `docs/plans/2026-06-14-swing-screener-design.md` (the 6 dashboard tabs).
**Builds on:** Phase 2 (`db/`, `pipeline/`, `charts/`) on `main`.

---

## Conventions

- New deps in `pyproject.toml` (`streamlit`). The dashboard package is `src/swing_screener/dashboard/`; analytics in `src/swing_screener/analytics/`.
- Logic is pure/tested; `app.py` only wires widgets to functions. The app reads its DB URL from the `SWING_DB_URL` env var (default `sqlite:///local.db`) so tests can point it at a temp DB.
- Tests never hit the network (mock the quote seam); DB tests use temp SQLite; the Streamlit smoke test uses `AppTest`.
- Each task: failing test → run (fail) → minimal impl → run (pass) → full gate (`ruff check src tests`, `mypy`, `pytest -q`) → commit. Work on a `phase3-dashboard` branch; PR at the end (CI gates merge).

---

## Task 1: Streamlit dep + app skeleton + headless smoke test

**Files:** `pyproject.toml`; `src/swing_screener/dashboard/__init__.py`; `src/swing_screener/dashboard/app.py`; `tests/dashboard/__init__.py`; `tests/dashboard/test_app_smoke.py`.

- Add `streamlit>=1.38` to `dependencies`; `pip install -e ".[dev]"`.
- `app.py`: read `SWING_DB_URL` from env (default `sqlite:///local.db`), `get_engine(url)` (creates tables — empty DB is fine), `st.title("Swing Screener")`, a sidebar showing the DB URL, and a `st.tabs([...])` with the six tab labels (bodies filled in later tasks). Each tab must render without error on an empty DB ("no data yet" placeholders).
- **Test** (`AppTest`): point `SWING_DB_URL` at a `tmp_path` sqlite, run `AppTest.from_file("src/swing_screener/dashboard/app.py").run()`, assert `not at.exception` and the title text is present.

Commit: `feat: streamlit dashboard skeleton + smoke test`.

## Task 2: Real-trade repository CRUD

**Files:** Modify `src/swing_screener/db/repo.py`; Test `tests/db/test_trade_repo.py`.

Add (mirroring the existing repo style, `select`/`commit`):
- `add_trade(session, trade: Trade) -> Trade` (add, commit, refresh).
- `get_open_trades(session) -> list[Trade]` (`status == "open"`).
- `get_closed_trades(session) -> list[Trade]` (`status == "closed"`, newest first by `exit_date`).
- `close_trade(session, trade_id, *, exit_date, exit_price, exit_reason) -> Trade` (load, set fields + `status="closed"`, commit).
- `update_trade(session, trade_id, **fields) -> Trade` (patch stop/target/size/notes, commit).

**Tests** (temp SQLite): round-trip add → open list; close → moves to closed list with exit fields; update mutates a field. Commit: `feat: real-trade repository CRUD`.

## Task 3: Unrealized P/L (pure)

**Files:** `src/swing_screener/dashboard/pl.py`; Test `tests/dashboard/test_pl.py`.

`position_pl(entry, stop, target, size, current_price) -> PositionPL` (frozen dataclass) for a long:
- `unrealized_pl = (current_price - entry) * size`
- `unrealized_pct = (current_price - entry) / entry`
- `r_multiple = (current_price - entry) / (entry - stop)`  (risk = entry - stop)
- `dist_to_stop_pct = (current_price - stop) / current_price`, `dist_to_target_pct = (target - current_price) / current_price`
Guard `entry - stop > 0` (raise `ValueError` otherwise — mirrors compute_zone's contract).

**Tests:** a winner (price above entry → positive P/L, R>0), a loser at the stop (R == -1), exact-value checks; the degenerate `entry<=stop` raises. Commit: `feat: unrealized P/L math`.

## Task 4: Quote helper (mockable, no network in tests)

**Files:** `src/swing_screener/dashboard/quotes.py`; Test `tests/dashboard/test_quotes.py`.

`latest_close(ticker, *, cache_dir) -> float | None` = last close of `fetch_bars(ticker, "1d", cache_dir=...)`, or None on failure (reuse the resilient fetch + cache). `latest_closes(tickers, *, cache_dir) -> dict[str, float]` skipping misses.

**Tests:** monkeypatch `fetch.fetch_bars` (or `fetch._download`) to return a small frame / None; assert the last close is returned and failures are skipped. No network. Commit: `feat: latest-close quote helper`.

## Task 5: Shadow-book performance analytics (pure)

**Files:** `src/swing_screener/analytics/__init__.py`; `src/swing_screener/analytics/performance.py`; Test `tests/analytics/test_performance.py`.

Pure functions over a sequence of `PaperTrade` rows (or lightweight dicts):
- `summarize(trades) -> PerformanceSummary` (frozen): `n_total`, `n_filled`, `fill_rate` (filled / (filled+missed+invalidated)), `n_closed`, `win_rate` (closed filled with realized_r > 0), `expectancy_r` (mean realized_r over closed), `profit_factor` (sum positive R / abs sum negative R; `inf` if no losers), `avg_hold_bars`.
- `breakdown(trades, key) -> dict[str, PerformanceSummary]` where `key` ∈ {"timeframe","quality_tier","volatility_tier","mtf_aligned"} and a `rank_bucket(trades, edges)` helper for rank-bucket slicing — this answers "does rank predict winners?".
- `equity_curve(trades) -> list[tuple[date, float]]` = cumulative realized R over closed trades ordered by `exit_date`.

**Tests** (crafted PaperTrade rows): a mix of wins/losses/missed → assert exact win_rate, expectancy_r, profit_factor, fill_rate; a breakdown by timeframe partitions correctly; equity curve is monotonic in count and sums to total R. Commit: `feat: shadow-book performance analytics`.

## Task 6: Wire the six tabs

**Files:** Modify `src/swing_screener/dashboard/app.py`; extend `tests/dashboard/test_app_smoke.py` (seed a temp DB, assert tabs render with data).

1. **Today's Candidates** — `repo.latest_signals(today)`; sidebar filters (timeframe/horizon/quality/volatility); per row: the annotated chart (`st.image(chart_path)` if present), entry zone / stop / target, score, MTF badge; a **"Take this trade"** `st.form` that pre-fills and calls `repo.add_trade`.
2. **Active Trades** — `repo.get_open_trades` + `quotes.latest_closes` + `pl.position_pl`: a table of live price, unrealized P/L ($ and %), size, entry, stop, target, distance-to-stop/target, and an exit-alert badge (reuse `evaluate_exit` against the latest bar; 🔴/🟠/🟡).
3. **Trade Entry / Management** — manual `st.form` (ticker, timeframe/horizon, entry, size, stop, target, notes) → `add_trade`; edit/close existing via `update_trade`/`close_trade`.
4. **Closed Trades** — `repo.get_closed_trades` table with per-trade and cumulative realized P/L.
5. **Screener Performance** — `analytics.summarize` headline metrics (`st.metric`), `breakdown`/`rank_bucket` bar charts (`st.bar_chart`), and the `equity_curve` (`st.line_chart`). The key view: win-rate / avg-R by rank bucket.
6. **Exit Log** — `exit_events` table (most recent first).

Keep each tab a thin function `def _render_candidates(session): ...`. Note: the **Claude-written rationale** on candidates is Phase 4 — Phase 3 shows the deterministic metrics + chart only.

**Test:** seed the temp DB with a Signal + an open Trade + a couple of closed PaperTrades, run `AppTest`, assert no exception and that key text (a ticker, a metric label) appears. Commit: `feat: dashboard tabs (candidates, trades, P/L, performance, exits)`.

## Task 7: Docs + finalize

- `docs/dashboard.md`: run with `streamlit run src/swing_screener/dashboard/app.py --server.address 127.0.0.1` (localhost-only, no public access); set `SWING_DB_URL` to the pipeline's DB; screenshots optional.
- Update the README roadmap (Phase 3 → done) and link the dashboard doc.
- Full gate green; final holistic review (superpowers:requesting-code-review); push `phase3-dashboard`, confirm CI green, open PR into `main`.

---

## Testing notes

- **Streamlit:** test logic in the pure modules; use `streamlit.testing.v1.AppTest` only for "renders without error + key content present" smoke coverage. Don't assert on pixel layout.
- **No network:** mock the quote/fetch seam; the dashboard must render fully offline from the DB (quotes degrade to "—" when unavailable).
- **DB:** every test uses a temp/`:memory:` DB via `SWING_DB_URL`; never the real `local.db`.

## Deferred to later phases

- Claude-written rationale on candidates and the email/PDF digests (Phase 4).
- Auto-refresh / websockets, broker auto-import, and any hosting (the dashboard stays a local, manually-refreshed Streamlit app; Azure is Phase 5 and the dashboard remains local, pointed at Azure SQL).

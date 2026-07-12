# Journal Layer v1 — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the platform Journal & observability layer v1 — P&L calendar, equity curve + drawdown, MAE/MFE excursion reports, discipline metrics, day-of-week/hold-time breakdowns, tags/mistakes with provenance, the notebook, and entry/exit thesis capture — against the swing module's **existing production data**, then a cockpit Journal view. Platform layer 4 of Meridian (see the suite's ARCHITECTURE.md / journal-layer-design.md, which land on main with the Meridian governance PR).

**Architecture:** A new `src/swing_screener/journal/` package of pure read-model + aggregation functions over the existing `PaperTrade`/`ExitEvent` tables, plus three small new tables (`journal_tags`, `journal_notes`, `journal_theses`) with provenance columns. Everything is R-native and unit-honest; nothing pools across books. The cockpit gets a Journal view — held until the in-flight cockpit Phase-3 restructure lands (same convergence rule as GEX Phase 1's cockpit tasks).

**Tech stack:** Python 3.12, pandas/SQLAlchemy 2.x, FastAPI, React 19 (no new deps).

**Environment:**
- Worktree: `.claude/worktrees/journal-v1`, branch `feat/journal-layer-v1` (off `origin/main` — independent of the GEX branch by design; its diff reviews cleanly on its own).
- `$PY = C:/Users/Oliver/source/repos/swing-screener/.venv/Scripts/python.exe`; run pytest from the worktree root (pythonpath=src).
- Gates each commit: `$PY -m pytest tests/journal/ -q`; full `$PY -m pytest -q` before each commit; `$PY -m ruff check src tests alembic`; `$PY -m mypy`.

**Grounding (fact-extraction 2026-07-12, verbatim):**
- `analytics/performance.py`: `equity_curve(trades) -> [(exit_date, cum_r)]` (line 553), `breakdown(trades, key)` groups by `str(getattr(t, key))` + summarize (line 402), `summarize() -> PerformanceSummary` (13 fields incl. `avg_hold_bars`, `expectancy_ci_low/high`, `n_clusters`, `thin_clusters`), `_clustered_ci_low(by_ticker, iid_low)` (cluster floor 8), `rank_bucket`/`score_bucket` (the bucketer template, lines 458/518), `cost_level_for(trades)` (line 65), `_is_closed_filled` (status closed + fill_status filled + realized_r not None).
- `cockpit/stats.py`: `Stat` dataclass — 10 fields `value,n,n_clusters,ci_low,ci_high,cost_level,corpus_id,facet,unit,thin_clusters`; `stat_from_summary(summary,*,cost_level,corpus_id,facet,unit="R")`. **The frontend has no bare-float renderer** — every statistic crosses the wire as a `Stat` dict.
- `db/models.py` `PaperTrade`: `realized_r`, **`low_water` (MAE raw price, line 153) + `high_water` (MFE raw price, line 183) — folded forward every bar by `shadow.py:364-369`, seeded to entry_price, NEVER consumed by analytics**, `risk`, `entry_price/exit_price/stop/target`, `exit_date/entry_date/opened_date`, `hold_bars`, `exit_reason`, `account` (research|paper|live), `partial_done/partial_r/remaining_frac`, `would_surface`, screener facets (quality_tier, vix_bucket, market_trend, …). `ExitEvent(trade_id, reason, tier, message, account)` is the append-only close stream. Dollar figures live only in `ExecutionLog` (notional, risk_dollars) — a different table.
- `repo.py`: `realized_r_on(session,*,run_date,account)` (line 356, per-day-per-book R sum), `load_closed_paper_trades` (pinned research), `load_research_paper_trades`.
- Cockpit: existing Performance panel wired `/api/stats/performance` (api.py:490, research-scoped) → `Stat` dicts → `PerformancePanel.tsx` → `Sparkline.tsx` (renders `[iso, R]`). New panels copy the `PanelBody`/`usePolling`/`useEventWake` template. SSE `_change_token` needs watermarks for new tables. Collision surface: `App.tsx` + committed static (with cockpit Phase-3 and GEX).

**Firewall / conventions:**
- **Never pool across books or units.** Swing is R-native; the `$`-denominated live-trade view (from `ExecutionLog`) is a separate, labeled surface — v1 does swing R only, no R/$ mixing.
- Provenance on every annotation: `source` ∈ `screener|analyst|human` on tags, notes, theses (the shadow-contamination lesson).
- MAE/MFE excursion capture needs **no new column and no backfill** — it's pure read-model math over `low_water`/`high_water`; legacy/unstepped rows are None and the reports must honor that.
- Events, not mutations: theses hang off the open (entry) and off `ExitEvent` (exit); notes are day-keyed.

**Coordination:** the cockpit Journal view (Tasks 13–14) collides with cockpit Phase 3 (unmerged, `feat/cockpit-phase3`) and GEX Phase 1's Task 18 on `App.tsx` + static. Build/commit the Journal **backend + API** first; land the UI after Phase 3, rebuilding the static once.

---

## Task 1: Excursions — MAE/MFE in R (the biggest "already there" win)

**Files:** Create `src/swing_screener/journal/__init__.py` (empty), `src/swing_screener/journal/excursions.py`, `tests/journal/__init__.py` (empty), `tests/journal/test_excursions.py`.

**Test contract:** `excursion_r(trade) -> Excursion(mae_r, mfe_r) | None` — `mae_r = (entry_price - low_water)/risk`, `mfe_r = (high_water - entry_price)/risk` for longs; None when any of `low_water/high_water/entry_price/risk` is None or risk == 0. `excursion_summary(trades) -> dict` over closed-filled trades with non-None excursions: `{n, avg_mae_r, avg_mfe_r, median_mae_r, median_mfe_r}`; empty → zeros. Golden test: a trade entered 100, stop 99 (risk 1), low_water 98.5, high_water 103 → mae_r 1.5, mfe_r 3.0.

**Steps:** failing test → implement pure functions (reuse `_is_closed_filled` from performance.py) → pass → ruff/mypy → commit `feat(journal): MAE/MFE excursions in R from existing low_water/high_water`.

## Task 2: Drawdown over the equity curve

**Files:** `src/swing_screener/journal/curve.py`, `tests/journal/test_curve.py`.

**Test contract:** `drawdown_series(curve: list[tuple[date, float]]) -> list[tuple[date, float]]` — running-max(cum_r) minus cum_r at each point (underwater curve, ≥0). `max_drawdown(curve) -> float`. Reuse `analytics.performance.equity_curve` to produce the input in an integration test (per-book by filtering `account` first). Golden: cum_r path [1,3,2,5,1] → drawdowns [0,0,1,0,4], max 4.

**Steps:** TDD → commit `feat(journal): drawdown + max-drawdown over the equity curve`.

## Task 3: P&L calendar (per book, R-native)

**Files:** `src/swing_screener/journal/calendar.py`, `tests/journal/test_calendar.py`.

**Test contract:** `pnl_calendar(trades, *, month: date | None) -> dict` — group closed-filled `realized_r` by `exit_date`, return `{"days": {iso_date: {"r": float, "n": int}}, "months": {"YYYY-MM": {"r": float, "n": int}}}`. Pure over a trade list (caller filters by account for per-book). Honor `cost_level_for(trades)` → include a `cost_level` field so a cell can show net-vs-mixed. Golden test with trades across two days/months.

**Steps:** TDD → commit `feat(journal): R-native P&L calendar aggregation`.

## Task 4: Day-of-week + hold-time breakdowns

**Files:** `src/swing_screener/journal/breakdowns.py`, `tests/journal/test_breakdowns.py`.

**Test contract:** `by_day_of_week(trades) -> dict[str, PerformanceSummary]` (derive weekday from `exit_date`, label Mon–Fri, reuse `summarize`). `by_hold_time(trades, edges=(1,3,5,10)) -> dict[str, PerformanceSummary]` (bucket on `hold_bars`, copy the `_bucket_trades_by_rank` label/membership pattern from performance.py:458). Every bucket present even when empty; each value a full clustered-CI `PerformanceSummary`. `by_symbol` is just `breakdown(trades, "ticker")` — a thin re-export, tested to confirm reuse.

**Steps:** TDD → commit `feat(journal): day-of-week and hold-time breakdowns`.

## Task 5: Discipline metrics

**Files:** `src/swing_screener/journal/discipline.py`, `tests/journal/test_discipline.py`.

**Test contract:** define over existing columns — `giveback_r` (mfe_r − realized_r, "left on table"), stop-honored rate (`exit_reason == "stop"` share), `avg_mae_before_win` (MAE-R on winners — did winners dip first?). `discipline_report(trades) -> dict` with these + counts, all None-safe. This is swing-side discipline; the GEX A+-rate/sat-on-hands metrics live in the GEX module (its checklist data). Keep swing metrics to what existing columns support; do NOT invent a checklist here.

**Steps:** TDD → commit `feat(journal): swing discipline metrics over existing trade columns`.

## Task 6: New tables — tags, notes, theses (with provenance)

**Files:** Modify `src/swing_screener/db/models.py`; create Alembic migration; `tests/journal/test_journal_models.py`.

**Schema (bounded strings; `source` provenance everywhere; events-not-mutations):**
- `JournalTag` — `id`, `kind` (String(16): `setup|mistake|context`), `name` (String(64)), `description` (String(256)).
- `JournalTradeTag` — join: `trade_id` (int, the PaperTrade id — no FK across the read-model boundary; index), `book` (String(16), the account, so the tag is unambiguous), `tag_id` (FK→journal_tags), `source` (String(16): `screener|analyst|human`), `created_at` (DateTime|None).
- `JournalNote` — `id`, `day` (Date, index), `kind` (String(16): `premarket|postmarket|adhoc`), `module` (String(16)|None), `body` (Text), `source` (String(16)), `created_at` (DateTime|None).
- `JournalThesis` — `id`, `trade_id` (int, index), `book` (String(16)), `event_kind` (String(8): `entry|exit`), `source` (String(16)), `body` (Text), `snapshot_json` (Text, default "{}"), `created_at` (DateTime|None). Entry thesis written at open (machine snapshot), exit thesis at settlement.

Migration follows the house style; `down_revision` = **whatever `alembic heads` reports at execution time** (the GEX migration `f2a9c4e7b1d8` and any cockpit-Phase-3 migration may be ancestors depending on merge order — pin at rebase time). Seed the mistake taxonomy (chased, moved_stop, oversized, early_exit, no_setup, revenge) in the migration or a repo helper.

**Steps:** TDD (in-memory `get_engine` roundtrip + a `source`-required assertion) → commit `feat(journal): tags, notes, theses tables with source provenance`.

## Task 7: Repo functions for the journal tables

**Files:** `src/swing_screener/journal/repo.py`, `tests/journal/test_journal_repo.py`.

**Test contract:** `add_tag`, `tag_trade(session, *, trade_id, book, tag_id, source)`, `list_trade_tags`, `add_note`, `notes_for_day`, `add_thesis`, `theses_for_trade`. Idempotency where it matters (a trade-tag pair is unique per source). Follows the plain-function `session`-first repo style; `source` is required (no default) so provenance is never silently lost.

**Steps:** TDD → commit `feat(journal): repo functions for tags/notes/theses`.

## Task 8: Mistake-cost report

**Files:** `src/swing_screener/journal/mistakes.py`, `tests/journal/test_mistakes.py`.

**Test contract:** `mistake_cost(session, trades) -> list[dict]` — join human/analyst mistake-tags to their trades, group by mistake name, sum realized_r and count ("chasing cost −4.2R over 6 trades"). Reuse `summarize` per group so each carries honest CIs. None/empty-safe.

**Steps:** TDD → commit `feat(journal): mistake-cost report`.

## Task 9: Unified TradeRecord read-model

**Files:** `src/swing_screener/journal/record.py`, `tests/journal/test_record.py`.

**Test contract:** `trade_records(session, *, book) -> list[TradeRecord]` — a display-only normalization over `PaperTrade` (v1: swing books). Fields: `book, module="swing", symbol, direction, opened, closed, unit="R", r, tags, theses`. **Display only — statistics stay per-book in the analytics fns; this never aggregates.** Designed so the GEX `OptionPaperTrade` folds in later as a second source (module="gex", unit varies) without changing consumers.

**Steps:** TDD → commit `feat(journal): unified TradeRecord read-model (swing source)`.

## Task 10: Journal API router (backend only — no UI yet)

**Files:** `src/swing_screener/cockpit/journal_api.py`; modify `cockpit/api.py` (router factory + include before static mount + `_change_token` watermarks for the three new tables); `tests/cockpit/test_journal_api.py`.

**Endpoints** (all `Stat`-dict for statistics, plain values for calendar cells/notes; 503-friendly; write endpoints require `X-Cockpit: 1`):
- `GET /api/journal/calendar?book=&month=` → calendar dict
- `GET /api/journal/curve?book=` → `{curve, drawdown, max_drawdown}`
- `GET /api/journal/excursions?book=` → excursion summary
- `GET /api/journal/breakdowns?book=&by=dow|hold|symbol` → `{buckets: {label: Stat}}`
- `GET /api/journal/discipline?book=` → discipline report
- `GET /api/journal/mistakes?book=` → mistake-cost list
- `GET /api/journal/notes?day=` / `POST /api/journal/notes`
- `POST /api/journal/trades/{book}/{trade_id}/tags` (tag a trade, source=human)
- `GET /api/journal/records?book=`

Follow the GEX router-factory pattern (closure over `_session`, `build_journal_router(session_dep)`), tested with the `test_api.py` `_db_url`/`_client` helper style. This is fully testable without any UI.

**Steps:** TDD each endpoint → commit `feat(journal): cockpit Journal API router + SSE watermarks`.

## Task 11: Full-suite + lint gate (backend checkpoint)

Run `$PY -m pytest -q` (all green), ruff, mypy. Commit any fixups. **This is the natural PR boundary for the backend half** — everything to here is engine + API with zero cockpit-UI collision, mergeable independent of Phase 3.

## Tasks 12–14: Cockpit Journal view — HELD until cockpit Phase 3 lands

Do NOT start until `feat/cockpit-phase3` merges (it restructures `App.tsx`). Then, as a module-shaped view (the `MODULES` registry the GEX plan introduces): a Journal screen with the calendar heatmap (new component — `Sparkline` won't cover a month grid), the equity+drawdown curve (reuse `Sparkline`), breakdown/discipline/excursion panels (reuse `StatChip`), the notebook editor, and per-trade tag controls. Rebuild + commit the static once. Written against the Task-10 API, which is already contract-tested — so the UI is pure presentation.

## Explicitly deferred to Journal v2

Session-Review coach, auto trade tagger, weaknesses profile, per-trade charts with markers (reuse `charts/`), Edge Score radar, the `$`-denominated ExecutionLog view, wiring GEX excursions/theses in (arrives with the GEX module's own data). See journal-layer-design.md.

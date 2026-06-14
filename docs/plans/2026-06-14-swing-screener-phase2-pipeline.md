# Swing Screener — Phase 2 (Data Pipeline + Shadow Book + Charts) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task. Build each component test-first per superpowers:test-driven-development.

**Goal:** Turn the Phase 1 engine into a runnable nightly pipeline on local SQLite: load a liquid universe, fetch + resample real bars, detect/score/rank signals across all timeframes, persist them, auto-paper-trade every signal into a self-grading shadow book, and render annotated Heiken Ashi charts.

**Architecture:** A linear orchestrator wraps the pure Phase 1 engine with three new layers — **data** (universe + yfinance fetch + resampling + cache), **persistence** (SQLAlchemy models on SQLite, same code will later point at Azure SQL), and **outputs** (mplfinance charts). Everything stays local this phase; a couple-week dry-run validates signal quality before Azure (Phase 5).

**Tech Stack:** Python 3.12, pandas, numpy (engine); SQLAlchemy 2.x + SQLite (persistence); yfinance (fetch, with Stooq fallback later); mplfinance (charts). pytest/ruff/mypy gate as in Phase 1.

**Design reference:** `docs/plans/2026-06-14-swing-screener-design.md`
**Builds on:** Phase 1 engine (`src/swing_screener/{indicators,signals,config}.py`), all on `main`.

---

## Conventions (carried from Phase 1)

- Engine functions stay **pure**; new I/O lives in clearly separated modules (`data/`, `db/`, `charts/`, `pipeline/`).
- New deps go in `pyproject.toml` `dependencies` (sqlalchemy, mplfinance) or `dev` as appropriate. **yfinance is a real runtime dep now** (the pipeline fetches), so add it to `dependencies`.
- I/O tasks are tested with mocks/fixtures + a temp SQLite DB + tmp_path — never hit the network in tests.
- Each task: failing test → run (confirm fail) → minimal impl → run (confirm pass) → full gate → commit. Work on a `phase2-pipeline` branch off `main`; PR at the end (CI gates the merge).

---

## Part A — Engine hardening (the Phase-1 review follow-ups, do first)

### Task 1: Guard `compute_zone` against degenerate zones

**Files:** Modify `src/swing_screener/signals/entry_zone.py`; Test `tests/signals/test_entry_zone.py`.

**Step 1 — Failing tests:** add cases asserting that an inverted/degenerate input raises a clear error (or is normalized). Decide the contract: raise `ValueError` when `atr <= 0` or when `trigger_close <= swing_low` (which would invert the zone).

```python
import pytest
from swing_screener.config import StrategyConfig
from swing_screener.signals.entry_zone import compute_zone

def test_non_positive_atr_raises():
    with pytest.raises(ValueError):
        compute_zone(trigger_close=100.0, atr=0.0, swing_low=96.0, cfg=StrategyConfig())

def test_trigger_below_swing_low_raises():
    with pytest.raises(ValueError):
        compute_zone(trigger_close=90.0, atr=4.0, swing_low=96.0, cfg=StrategyConfig())
```

**Step 2:** run → fail. **Step 3:** add a guard at the top of `compute_zone`:

```python
    if atr <= 0:
        raise ValueError(f"atr must be positive, got {atr}")
    if trigger_close <= swing_low:
        raise ValueError(f"trigger_close ({trigger_close}) must exceed swing_low ({swing_low})")
```

**Step 4:** run → pass (existing tests still pass; the detector only ever calls this with valid inputs). **Step 5:** full gate. **Step 6:** commit `fix: guard compute_zone against degenerate inputs`.

### Task 2: Parameterize `evaluate_exit`'s `bar`

**Files:** Modify `src/swing_screener/signals/exits.py`; Test add to `tests/signals/test_exits.py`.

Replace bare `Mapping` with `Mapping[str, float | bool]` so the orchestrator contract is explicit, and confirm a real `pd.Series` row still satisfies it at runtime. Add a test passing a `pd.Series` (a real `build_frame` row) to `evaluate_exit` and asserting it behaves. Keep mypy clean. Commit `refactor: type evaluate_exit bar contract`.

### Task 3: `ScoreInputs` bridge (wire detection → scoring)

**Files:** Create `src/swing_screener/signals/build_score.py`; Test `tests/signals/test_build_score.py`.

A pure function that turns a `PullbackContext` + the last frame row into `ScoreInputs`:

```python
def build_score_inputs(ctx: PullbackContext, last_row: Mapping[str, float | bool],
                       mtf_aligned: bool) -> ScoreInputs:
    price = ctx.trigger_close
    trend_slope = (last_row["ema_fast"] - last_row["ema_slow"]) / price
    atr_pct = ctx.atr / price
    return ScoreInputs(shaved_bottom=ctx.shaved_bottom, body_frac=float(last_row["body_frac"]),
                       trend_slope=trend_slope, atr_pct=atr_pct, mtf_aligned=mtf_aligned)
```

Test that a known ctx/row yields the expected `ScoreInputs` and that `score_signal(build_score_inputs(...))` is in [0,1]. This closes the gap where `score_signal` had no producer and `PullbackContext.rsi` was unused (rsi will feed the oversold tag in Task 9). Commit `feat: ScoreInputs bridge from detection context`.

---

## Part B — Data layer

### Task 4: Universe seed + loader

**Files:** Create `src/swing_screener/data/__init__.py`, `src/swing_screener/data/universe.py`, `src/swing_screener/data/universe_seed.csv` (committed list of ~1–2k liquid tickers: S&P 500 + Russell 1000, columns `ticker,name,exchange`); Test `tests/data/test_universe.py`.

`load_universe(path=...) -> list[UniverseEntry]` (frozen dataclass: ticker, name, exchange). Test loads a small fixture CSV and asserts parsing + dedupe + uppercase tickers. (Generate the real seed CSV via a one-off `scripts/make_universe_seed.py` that pulls the S&P 500 / Russell 1000 constituents; commit the CSV so the pipeline is offline-deterministic.) Commit `feat: universe seed + loader`.

### Task 5: Timeframe resampling (pure)

**Files:** Create `src/swing_screener/data/resample.py`; Test `tests/data/test_resample.py`.

Pure `resample_ohlcv(df, rule) -> df` using pandas `resample(rule).agg({open:first, high:max, low:min, close:last, volume:sum}).dropna()`. Support `"4h"` (from 1h), `"1W"`, `"1ME"` (from 1d). Test that resampling a known intraday/daily frame yields correct OHLCV aggregation and the index aligns to period boundaries. This is the multi-timeframe foundation. Commit `feat: ohlcv resampling`.

### Task 6: yfinance fetch with isolation + cache

**Files:** Create `src/swing_screener/data/fetch.py`; Test `tests/data/test_fetch.py`.

`fetch_bars(ticker, interval) -> df | None` wrapping yfinance with: retry-with-backoff, **per-ticker isolation** (catch + log, return None on failure — never raise), and a parquet cache keyed by (ticker, interval, date) under a cache dir. `fetch_universe(tickers, interval) -> dict[str, df]` skips failures. **Tests mock yfinance** (monkeypatch `fetch._download`) — assert: a failing ticker returns None and doesn't kill the batch; a cached read avoids a second download. No network in tests. Commit `feat: resilient yfinance fetch with cache`.

---

## Part C — Persistence (SQLAlchemy on SQLite)

### Task 7: DB models + session

**Files:** Create `src/swing_screener/db/__init__.py`, `src/swing_screener/db/models.py`, `src/swing_screener/db/session.py`; Test `tests/db/test_models.py`.

SQLAlchemy 2.x declarative models matching the design's schema: `Universe`, `Signal` (ticker, timeframe, run_date, metrics, score, rank, entry floor/ceiling/stop/target, chart paths), `Trade`, `PaperTrade` (+ QC fields: signal score, rank, mtf_aligned, fill_status, realized_r, hold_bars, exit_reason), `ExitEvent`, `EmailLog`. `get_session(url="sqlite:///...")` factory; `create_all`. Test against an in-memory SQLite (`sqlite:///:memory:`): create tables, insert + query a Signal and a PaperTrade round-trip. Commit `feat: sqlalchemy models + session`.

### Task 8: Signal repository

**Files:** Create `src/swing_screener/db/repo.py`; Test `tests/db/test_repo.py`.

Thin CRUD helpers: `save_signals(session, signals)`, `open_paper_trades(session, ...)`, `load_open_paper_trades(session)`, `record_exit(session, ...)`, `latest_signals(session, run_date)`. Test round-trips on a temp SQLite (`tmp_path`). Keep queries simple and typed. Commit `feat: signal/trade repository`.

---

## Part D — Orchestration + shadow book

### Task 9: Per-ticker analysis + MTF alignment + scoring

**Files:** Create `src/swing_screener/pipeline/analyze.py`; Test `tests/pipeline/test_analyze.py`.

`analyze_ticker(ticker, bars_by_tf, cfg) -> list[SignalResult]`: for each timeframe build the frame, `detect_last_bar`; if a ctx fires, compute the entry zone, the categorization tags (horizon from tf; quality from price + avg dollar volume; volatility from atr_pct; oversold from `ctx.rsi`), compute **MTF alignment** (does the next-higher tf's frame show `ema_fast>ema_slow & close>ema_slow`?), then `build_score_inputs` + `score_signal`. Returns structured results. Test with synthetic multi-timeframe fixtures (reuse Task 6-style builders) asserting: a fired signal carries a zone + score; MTF alignment boosts the score; tags are set. Commit `feat: per-ticker analysis with MTF alignment + scoring`.

### Task 10: Shadow book (worst-case fills + exit advancement)

**Files:** Create `src/swing_screener/pipeline/shadow.py`; Test `tests/pipeline/test_shadow.py`.

Two pure-ish functions over data + DB:
- `open_from_signals(session, signals, next_bars)` — for every signal, `resolve_fill` on the next bar (worst-case in-zone); record `filled`/`missed`/`invalidated`; for filled, open a PaperTrade risk-normalized to 1R.
- `advance_open(session, latest_bars, cfg)` — for each open paper trade, `evaluate_exit`; on EXIT, close it and record realized R, hold bars, exit reason; hard stop closes unconditionally.

Test on temp SQLite with crafted bars: a signal that gaps above ceiling → recorded missed (no trade); one that fills → open; a subsequent bar breaching stop → closed at hard stop with negative R. This is the screener-QC core — test it thoroughly. Commit `feat: shadow book fills + exit advancement`.

### Task 11: Annotated HA chart rendering

**Files:** Create `src/swing_screener/charts/render.py`; Test `tests/charts/test_render.py`.

`render_chart(frame, ctx, zone, out_path) -> Path` using mplfinance: HA candles, EMA20/50 overlays, shaded pullback window, shaded entry zone band, stop/target hlines, marked trigger bar. Test asserts the PNG file is created and non-empty (`out_path.stat().st_size > 0`) for a sample frame; use a non-interactive matplotlib backend (`matplotlib.use("Agg")`). Don't assert pixels. Commit `feat: annotated heiken ashi chart rendering`.

### Task 12: The nightly orchestrator

**Files:** Create `src/swing_screener/pipeline/run.py` (CLI entry: `python -m swing_screener.pipeline.run`); Test `tests/pipeline/test_run.py`.

Wires it end to end: load universe → fetch bars (4h via 1h resample, 1d, 1wk, 1mo) → `analyze_ticker` across the universe → rank all hits (score desc) with that-night rank → `save_signals` → render charts for the top N → shadow book `open_from_signals` + `advance_open`. Config via env/args (DB url, cache dir, universe path, top-N for charts). Test with a **mocked fetch** returning a tiny 2–3 ticker fixture + temp SQLite + tmp chart dir: assert signals are persisted, paper trades opened, ranks assigned, charts written. Graceful per-ticker isolation end-to-end (one bad ticker doesn't abort the run). Commit `feat: nightly orchestrator pipeline`.

---

## Part E — Dry run + finish

### Task 13: Dry-run script, docs, finalize

- Add `scripts/run_local.py` (or document `python -m swing_screener.pipeline.run --db sqlite:///local.db`) and a short `docs/running-locally.md`.
- Run the real pipeline once on the live universe (small slice first, e.g. 50 tickers) to sanity-check signal volume and chart output; capture a couple of real charts to eyeball against the strategy.
- Full gate: `ruff check src tests`, `mypy`, `pytest -q` all green.
- Final holistic review (superpowers:requesting-code-review), push `phase2-pipeline`, confirm CI green, open PR into `main` (superpowers:finishing-a-development-branch).
- **Then: a 1–2 week local dry-run accumulating the shadow book before Phase 3 (dashboard).**

---

## Testing notes for the executor

- **Never hit the network in tests** — monkeypatch the yfinance download seam; use committed CSV/parquet fixtures.
- **DB tests** use `sqlite:///:memory:` or `tmp_path` files; never a shared on-disk DB.
- **Charts** use the Agg backend; assert file creation, not appearance.
- Keep the engine pure; all new randomness/clock/network is injected so tests are deterministic.
- Carry the Phase-1 tuning knob forward: revisit the direction-agnostic `zone` flag and promote score weights to `StrategyConfig` if the dry-run signal quality warrants it.

## What Phase 2 deliberately defers

- Dashboard (Phase 3), email + LLM analysis (Phase 4), Azure deploy + CD (Phase 5), options (later). No real-trade entry UI yet — `Trade` table exists but is populated in Phase 3.

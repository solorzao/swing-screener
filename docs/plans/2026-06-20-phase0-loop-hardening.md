# Phase 0 — Loop-statistics hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the self-optimizing loop's numbers *honest* — so the optimizer, the leaderboard, the auto-proposal, and (later) the edge-file "confirmed edge" grading rest on net-of-cost, correlation-aware, multiple-comparisons-resistant inference instead of a gross optimistic-fill upper bound certified by an IID normal-approximation CI.

**Architecture:** Four additive changes on current `main`, smallest-blast-radius first: (1) expose the replay trade-level book + lock it with no-lookahead/golden-master tests; (2) a default-0.0 fill-cost haircut on level fills; (3) a ticker-clustered bootstrap lower bound replacing the IID CI inside `summarize` (so every consumer — `leaderboard_order`, `propose`, the dashboard — inherits it); (4) real teeth in `propose()` — a ticker-clustered **two-sample** winner-vs-incumbent delta test + a label-shuffle placebo + a distinct-ticker floor + a provenance log. Governed by [North Star](../NORTH_STAR.md) principles #1 (evidence over narrative) and #2 (honest about uncertainty). This is Phase 0 of [the learning-loop design](2026-06-20-learning-loop-design.md).

**Tech Stack:** Python 3.12, numpy (already a dep — used for the bootstrap), pandas, SQLAlchemy, pytest. **No new dependencies.**

---

## Design decisions (read before starting)

- **D1 — Variants are NOT same-sample; arms ARE.** The optimizer sweeps *variants* (different entry/screen configs → different fills), so `propose()`'s winner-vs-incumbent comparison is a **two-sample** difference of independent books, **clustered by ticker** — NOT a per-fill paired test. (A paired test is correct only for the same-sample *arm* A/B; that's a later, separate use.) Getting this right is the point of Task 4.
- **D2 — The haircut is fixed a-priori and never swept.** `fill_slippage_atr` models microstructure (slippage/spread on a level fill). It is set once from a rule, defaults to `0.0` (a strict no-op, byte-identical to today), and is **never** added to `build_config_grid` — sweeping it would re-introduce the data-snooping it removes (North Star #2, #8).
- **D3 — The clustered bound replaces the CI inside `summarize`, so it flows everywhere.** `leaderboard_order`, `propose`, and the dashboard all read `PerformanceSummary.expectancy_ci_low`; hardening that one computation hardens the whole loop with no call-site changes. The bound is `min(IID_lower, clustered_lower)` so it can never read *more* optimistic than today, with a distinct-ticker floor below which it falls back to IID and is flagged thin.
- **D4 — Determinism.** Every bootstrap uses a seeded `numpy.random.default_rng(seed)`; the weekly GitHub Action and the tests must be reproducible. No `Date.now()`-style nondeterminism.
- **D5 — Levels stay ground truth.** Nothing here touches entry/stop/target computation; the haircut changes only the *recorded fill price* of a level exit, never the level itself.

**Commit discipline:** after every green task run the CI gate — `.venv\Scripts\python.exe -m ruff check .`, `... -m mypy`, `... -m pytest -q` — before committing. Use `py -3.12` / the `.venv`.

---

## Task 1: Expose the replay book + lock it (no-lookahead + golden-master)

Unlocks trade-level testing (and Task 4's delta test), and locks the causal contract before anything perturbs the numbers. **No behavior change.**

**Files:**
- Modify: `src/swing_screener/pipeline/replay.py` (refactor `replay` to sit on a new `replay_book`)
- Test: `tests/pipeline/test_replay_no_lookahead.py` (create), extend `tests/pipeline/test_replay.py`

**Step 1 — Refactor `replay` to expose the book (behavior-preserving).** In `replay.py`, factor the walk out of `replay` so the raw trades are reachable:

```python
def replay_book(
    frames: Mapping[str, pd.DataFrame], *, timeframe: str,
    base_cfg: StrategyConfig | None = None,
    variants: Mapping[str, StrategyConfig] | None = None,
) -> list[PaperTrade]:
    """Walk each ticker forward and return the raw shadow-book PaperTrades (detached).
    The trade-level substrate the leaderboard, no-lookahead test, and propose() delta
    test all read. Behavior-identical to the loop previously inlined in replay()."""
    base_cfg = base_cfg or StrategyConfig()
    variants = variants or build_screen_variants(base_cfg)
    warmup = _warmup(base_cfg)
    with tempfile.TemporaryDirectory() as tmp:
        engine = get_engine(f"sqlite:///{Path(tmp) / 'replay.db'}")
        with Session(engine) as s:
            for ticker, raw in frames.items():
                if raw is None or len(raw) <= warmup:
                    continue
                enriched = build_frame(raw, base_cfg)
                _replay_one(s, ticker, timeframe, enriched, base_cfg, variants, warmup)
            trades = list(s.scalars(select(PaperTrade)))
            for t in trades:                      # detach so callers outlive the session
                s.expunge(t)
            return trades


def replay(
    frames: Mapping[str, pd.DataFrame], *, timeframe: str,
    base_cfg: StrategyConfig | None = None,
    variants: Mapping[str, StrategyConfig] | None = None,
) -> dict[str, PerformanceSummary]:
    """Walk history forward and rank screen variants. Thin wrapper over replay_book."""
    return breakdown(replay_book(frames, timeframe=timeframe, base_cfg=base_cfg,
                                 variants=variants), "variant")
```

(`expunge` detaches without a DB round-trip; the trades are read-only snapshots after the session closes. If a later attribute read trips a lazy load, switch to copying the needed columns — but PaperTrade has no relationships, so `expunge` suffices.)

**Step 2 — Run the existing replay tests** (`pytest tests/pipeline/test_replay.py -q`): all must still pass unchanged (proves the refactor is behavior-identical).

**Step 3 — Golden-master test.** Snapshot the book over the AMD 2018 fixture at the default config so any *accidental* behavior drift in Tasks 2–4 trips an alarm (the haircut defaults to 0.0 and the CI/gate changes don't change which trades happen, so this stays green):

```python
# tests/pipeline/test_replay_no_lookahead.py
from pathlib import Path
import pandas as pd
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import replay_book

FIXTURE = Path(__file__).parents[1] / "fixtures" / "amd_daily_2018.csv"

def _amd():
    df = pd.read_csv(FIXTURE, index_col=0, parse_dates=True)
    return df[["open", "high", "low", "close", "volume"]].astype(float)

def _key(t):
    return (t.ticker, t.variant, t.opened_date, round(t.entry_price or 0, 4),
            round(t.stop, 4), round(t.target, 4), t.exit_reason,
            None if t.realized_r is None else round(t.realized_r, 4))

def test_replay_book_is_stable_golden_master():
    book = sorted(_key(t) for t in replay_book({"AMD": _amd()}, timeframe="1d"))
    # Snapshot: regenerate ONCE with `-s` printing, paste, then freeze. Any later
    # accidental behavior drift (vs an intended, separately-asserted change) trips this.
    assert len(book) >= 1
    # EXPECTED = [...]  # paste the captured tuples here on first green run
    # assert book == EXPECTED
```
(Capture `EXPECTED` on the first run, paste it in, and uncomment — the implementer does this once the refactor is green.)

**Step 4 — No-lookahead spike test (the load-bearing one).** A future bar cannot change a past decision. Build a deterministic frame, replay it, then mutate a bar far in the future and assert every trade opened/closed *before* that bar is byte-identical:

```python
import numpy as np

def _synth(n=400):
    idx = pd.bdate_range("2022-01-01", periods=n)
    t = np.arange(n)
    close = 50 + 0.08 * t + 1.5 * np.sin(t / 8.0)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.05
    low = np.minimum(open_, close) - 0.4
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 1e6)}, index=idx)

def test_future_bar_cannot_change_past_trades():
    cfg = StrategyConfig(max_extension_atr=0.0)   # gate off so the synth fires setups
    full = _synth(400)
    spiked = full.copy()
    j = 360
    spiked.iloc[j:, spiked.columns.get_loc("high")] *= 5.0   # absurd future spike at/after j
    spiked.iloc[j:, spiked.columns.get_loc("close")] *= 5.0
    cutoff = full.index[j - 2].date()

    def opened_by(frame):
        return {_key(t): t for t in replay_book({"S": frame}, timeframe="1d",
                base_cfg=cfg, variants={"default": cfg})
                if t.opened_date is not None and t.opened_date <= cutoff}

    base, mutated = opened_by(full), opened_by(spiked)
    shared = set(base) & set(mutated)
    assert shared, "expected pre-spike trades to compare"
    assert set(base) == set(mutated)   # identical key set: same fills + same closed economics
```
Verify it has teeth: temporarily change `_replay_one`'s `prior = through.iloc[:-1]` to `through` (a 1-bar lookahead) and confirm this test FAILS, then restore.

**Step 5 — Gate + commit:** `feat(replay): expose replay_book + no-lookahead & golden-master tests`.

---

## Task 2: Fill-cost haircut (default-0.0, byte-identical)

**Files:**
- Modify: `src/swing_screener/config.py` (add one field near the `# exits` block, ~line 80)
- Modify: `src/swing_screener/pipeline/shadow.py` (`advance_open`: define `slip` after line 183; apply at 224-225 and 238-241)
- Test: `tests/pipeline/test_shadow_slippage.py` (create)

**Step 1 — Failing tests.** Build an open `PaperTrade`, advance it one bar onto a stop / a target, and assert the realized R drops by exactly `k * atr / risk` when `fill_slippage_atr=k`, and is byte-identical at `0.0`. (Model the test on `tests/pipeline/test_shadow*` fixtures — construct the trade + a bar dict with a known `atr`, call `advance_open` against an in-memory session.) Cover: stop exit, target exit (all-or-nothing arm, `partial_frac=0`), the partial leg (`partial_frac>0`), momentum/time exits get **no** haircut, and the `atr<=0` guard skips the haircut.

**Step 2 — Run, verify fail.**

**Step 3 — Implement.** In `config.py`:
```python
    # Fill-pessimism haircut on LEVEL exits (stop/target/partial), as a fraction of ATR.
    # Models that a level fill executes slightly worse than the exact level (slippage/
    # gap-through). 0.0 = OFF (exact-level fills, the historical default; a strict no-op).
    # Fixed a-priori from a microstructure rule -- NEVER added to the optimizer grid.
    # momentum_flip/time_stop exits use the bar close and are NOT haircut.
    fill_slippage_atr: float = 0.0
```
In `shadow.py`, right after `atr_val = float(bar.get("atr", 0.0))` (line 183):
```python
        # Fill-pessimism haircut on LEVEL fills (worse for a long). 0 when off or atr undefined.
        slip = (
            cfg.fill_slippage_atr * atr_val
            if (cfg.fill_slippage_atr > 0.0 and atr_val > 0.0)
            else 0.0
        )
```
Partial leg (224-225):
```python
            pt.partial_price = pt.target - slip
            pt.partial_r = (pt.partial_price - pt.entry_price) / pt.risk
```
Exit block (238-241): `exit_price = pt.stop - slip` for `stop`, `pt.target - slip` for `target`; the `else` (close) branch unchanged.

**Step 4 — Run, verify pass; the Task 1 golden-master stays green** (default 0.0 = no change).

**Step 5 — Commit:** `feat(shadow): optional ATR fill-cost haircut on level exits (default off)`.

---

## Task 3: Ticker-clustered bootstrap lower bound in `summarize`

Replace the IID normal-approx lower bound with a correlation-aware one that every consumer inherits.

**Files:**
- Modify: `src/swing_screener/analytics/performance.py` (`summarize`, lines 58-109; add a helper)
- Test: extend `tests/analytics/test_performance.py`

**Step 1 — Failing tests.** Assert: (a) when one ticker dominates the realized R (e.g. 18 trades on AAA, 2 on others, AAA strongly positive), the clustered `expectancy_ci_low` is **materially below** the IID lower bound (the clustering widens it); (b) deterministic for a fixed seed; (c) with fewer than the distinct-ticker floor, it falls back to IID and sets a `thin_clusters=True` flag; (d) it is never *above* the IID lower bound (`min` rule).

**Step 2 — Run, verify fail.**

**Step 3 — Implement.** Add to `PerformanceSummary` a `n_clusters: int` and `thin_clusters: bool` field, and compute the clustered bound in `summarize` (it already receives `PaperTrade`s, which carry `.ticker`):

```python
import numpy as np

_CLUSTER_FLOOR = 8          # distinct tickers below which the clustered bound is untrusted
_N_BOOT = 1000              # bootstrap resamples (numpy-vectorized; tune if needed)
_BOOT_SEED = 12345          # determinism (D4)

def _clustered_ci_low(by_ticker: dict[str, list[float]], point: float,
                      iid_low: float) -> tuple[float, int, bool]:
    """Ticker-clustered bootstrap lower 2.5% bound on mean R. Resample TICKERS with
    replacement (so within-ticker correlation is respected), pool their trades, take the
    mean; the 2.5th percentile is the lower bound. Returns min(iid_low, clustered_low) so
    it never reads more optimistic; falls back to iid_low (flagged thin) below the floor."""
    tickers = list(by_ticker)
    n_clusters = len(tickers)
    if n_clusters < _CLUSTER_FLOOR:
        return iid_low, n_clusters, True
    rng = np.random.default_rng(_BOOT_SEED)
    idx = np.arange(n_clusters)
    pools = [np.array(by_ticker[t], dtype=float) for t in tickers]
    means = np.empty(_N_BOOT)
    for b in range(_N_BOOT):
        pick = rng.choice(idx, size=n_clusters, replace=True)
        means[b] = np.concatenate([pools[i] for i in pick]).mean()
    clustered_low = float(np.percentile(means, 2.5))
    return min(iid_low, clustered_low), n_clusters, False
```
In `summarize`, after computing `expectancy_ci_low` (the IID value, line 82), group the closed realized R by ticker and override the lower bound:
```python
    by_ticker: dict[str, list[float]] = defaultdict(list)
    for t in closed:
        if t.realized_r is not None:
            by_ticker[t.ticker].append(t.realized_r)
    iid_low = expectancy_r - _Z95 * expectancy_stderr
    if n_closed >= 2:
        expectancy_ci_low, n_clusters, thin_clusters = _clustered_ci_low(
            by_ticker, expectancy_r, iid_low)
    else:
        expectancy_ci_low, n_clusters, thin_clusters = iid_low, len(by_ticker), True
```
Keep `expectancy_ci_high` as the IID upper bound (the lower bound is what every gate uses). Thread `n_clusters`/`thin_clusters` into the returned `PerformanceSummary`.

**Performance note:** `summarize` is called per breakdown group (dashboard, score_bucket, regime). `_N_BOOT=1000` × small groups is fine for a local Streamlit + offline replay; if a hot path shows lag, lower `_N_BOOT` or memoize. Do NOT silently skip the bootstrap.

**Step 4 — Run, verify pass.** `leaderboard_order` and `propose` now consume the hardened bound automatically — re-run their tests; some fixtures may need more distinct tickers to stay "trusted" (update fixtures, not the assertions' intent).

**Step 5 — Commit:** `feat(analytics): ticker-clustered bootstrap lower bound replaces IID CI`.

---

## Task 4: Real teeth in `propose()` — clustered two-sample delta + placebo

**Files:**
- Modify: `src/swing_screener/pipeline/optimize.py` (carry per-variant OOS trades on `OptimizeResult`)
- Modify: `src/swing_screener/pipeline/propose.py` (the gate, lines 44-87; new stats helpers)
- Test: extend `tests/pipeline/test_propose.py`

**Step 1 — Carry the trades.** `optimize()` currently keeps only summaries. Add the OOS books so `propose` can test the difference. In `optimize.py`, capture the trade-level book per split using `replay_book` (Task 1) and store `out_of_sample_trades: dict[str, list[PaperTrade]]` on `OptimizeResult` (group `replay_book(out_frames, ...)` by `variant`). Keep `in_sample`/`out_of_sample` summaries as-is.

**Step 2 — Failing tests** (extend `test_propose.py`). Add cases: (a) winner beats incumbent on the OOS *point* estimate but the **clustered two-sample delta CI includes 0** → no proposal; (b) winner's OOS spans **fewer than the distinct-ticker floor** → no proposal; (c) the **label-shuffle placebo** is not cleared (observed delta within the shuffled null) → no proposal; (d) a genuine, ticker-diverse, delta-positive winner → proposes, and the body contains a **provenance** line (grid hash, git SHA, n_configs). Build books with explicit per-ticker trades (extend the `_summary` helper to a `_trades(ticker_to_rs)` builder).

**Step 3 — Implement the delta + placebo (D1: two-sample, clustered, NOT paired).**
```python
import numpy as np

def _clustered_two_sample_delta_low(
    winner: list[PaperTrade], incumbent: list[PaperTrade], *, seed: int = 12345,
    n_boot: int = 1000,
) -> float:
    """Lower 2.5% bound on (mean winner R - mean incumbent R), resampling TICKERS with
    replacement INDEPENDENTLY in each book (variants are not same-sample, so this is a
    two-sample clustered bootstrap, not a paired one -- see design D1)."""
    def by_ticker(ts):
        d: dict[str, list[float]] = {}
        for t in ts:
            if t.status == "closed" and t.fill_status == "filled" and t.realized_r is not None:
                d.setdefault(t.ticker, []).append(t.realized_r)
        return d
    w, i = by_ticker(winner), by_ticker(incumbent)
    if not w or not i:
        return float("-inf")
    rng = np.random.default_rng(seed)
    wt, it = list(w), list(i)
    deltas = np.empty(n_boot)
    for b in range(n_boot):
        wm = np.concatenate([w[wt[k]] for k in rng.integers(0, len(wt), len(wt))]).mean()
        im = np.concatenate([i[it[k]] for k in rng.integers(0, len(it), len(it))]).mean()
        deltas[b] = wm - im
    return float(np.percentile(deltas, 2.5))


def _placebo_cleared(
    winner: list[PaperTrade], incumbent: list[PaperTrade], observed_delta: float,
    *, seed: int = 12345, n_shuffle: int = 1000,
) -> bool:
    """Shuffle the book labels (pool both, randomly relabel winner/incumbent preserving
    sizes) and confirm the observed delta exceeds the 95th percentile of the shuffled
    null. If a random relabel reproduces the edge, it is an artifact."""
    pool = [t.realized_r for t in (winner + incumbent)
            if t.status == "closed" and t.fill_status == "filled" and t.realized_r is not None]
    nw = sum(1 for t in winner
             if t.status == "closed" and t.fill_status == "filled" and t.realized_r is not None)
    if nw == 0 or nw == len(pool):
        return False
    arr = np.array(pool, dtype=float)
    rng = np.random.default_rng(seed)
    null = np.empty(n_shuffle)
    for b in range(n_shuffle):
        perm = rng.permutation(arr)
        null[b] = perm[:nw].mean() - perm[nw:].mean()
    return observed_delta > float(np.percentile(null, 95))
```
Extend `propose()`'s gate (after the existing point-estimate checks) to require, using `result.out_of_sample_trades`:
- a **distinct-ticker floor** on the winner's OOS book (`>= _CLUSTER_FLOOR` distinct tickers),
- `_clustered_two_sample_delta_low(winner, incumbent) > 0`,
- `_placebo_cleared(winner, incumbent, observed_delta=w_oos.expectancy_r - inc_oos_expectancy)`.

And add a **provenance** block to the proposal body: the grid (sorted variant names) hashed, the current `git rev-parse HEAD` (via `subprocess`, guarded — `"unknown"` on failure), and `n_configs = len(result.in_sample)`. This makes the researcher-degrees-of-freedom auditable.

**Step 4 — Run, verify pass** (incl. the existing propose tests, updated for the richer `OptimizeResult`).

**Step 5 — Commit:** `feat(propose): clustered two-sample delta + placebo + distinct-ticker + provenance gate`.

---

## Definition of done

- `ruff check . && mypy && pytest -q` all green; CI green on the PR.
- The no-lookahead test has verified teeth (fails under an injected 1-bar leak); the golden-master is frozen.
- The default-0.0 haircut is byte-identical (golden-master + the existing suite unchanged).
- `expectancy_ci_low` everywhere is now `min(IID, clustered)` with a distinct-ticker floor; `leaderboard_order` and `propose` inherit it.
- `propose()` no longer fires on a point-estimate difference alone: it requires a clustered two-sample delta lower bound > 0, a cleared placebo, a distinct-ticker floor, and logs provenance.
- **First payoff to note in the PR:** re-audit the already-shipped arm A/B and regime conclusions under the hardened bound — some may have been "trusted" only by the weaker IID CI.

## Out of scope (later phases / deferred per the design + analysis)

Heavy Bonferroni/effective-N correction and multi-fold nested CV (defer until the grid actually widens), MTF-aligned replay, the edge files + reflection (Phase 1), the insight engine (Phase 2), execution adapters (Phase 3+). Do not widen `build_config_grid` here.

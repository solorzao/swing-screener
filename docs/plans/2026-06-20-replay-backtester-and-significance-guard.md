# Replay Backtester + A/B Significance Guard — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the two-part linchpin that makes every future data-driven improvement honest: (Step 1) a statistical significance/margin guard over the existing parallel-arm A/B, and (Step 2) a deterministic 1d replay backtester that manufactures a large historical labeled trade set by reusing the live engine verbatim.

**Architecture:**
- **Step 1 (guard)** is pure analytics: a new `analytics/significance.py` that takes closed `PaperTrade`s and renders a verdict per challenger arm vs `baseline` using a **ticker-clustered paired bootstrap** + a **margin** over optimistic fills + a **multiple-comparisons** correction. Plus two thin loaders in `db/repo.py` (today only `load_open_paper_trades` exists). Wired into the dashboard arm A/B view. Independently useful **today** — the live arm A/B can currently crown false winners.
- **Step 2 (replay)** is an offline driver `pipeline/replay.py` that, per ticker, enriches the 2y daily frame **once** (every indicator is causal `ewm(adjust=False)`, so `F.iloc[:i]` is a valid point-in-time slice) and slides a cursor `i`, at each step reproducing one live "night": detect on the prior slice, fill against bar `i`, advance opens against bar `i`. It calls the **exact** live functions (`analyze_frames`, `analyze_reversals`, `open_from_signals`, `advance_open`, `_bar_row`) against a **throwaway in-memory SQLite** — never a re-implemented twin. Output feeds the Step 1 guard.

**Tech Stack:** Python 3.12, pandas, numpy (already deps), SQLAlchemy 2.x, pytest. No new dependencies.

---

## Design decisions (read before starting)

These were settled in the adversarial analysis; they are defaults the executor may revisit but should not silently drop:

- **D1 — Replay is 1d-only.** Weekly (~104 bars/ticker) and monthly (~24) are statistically undecidable; 4h is capped at ~60 days by the intraday fetch ([run.py:143](../../src/swing_screener/pipeline/run.py)). All replay claims transfer to the **1d engine only**. Higher timeframes are still **built per step** purely to reproduce the `mtf_aligned` score factor faithfully (see D2) but no 1wk/1mo trades are filled.
- **D2 — MTF fidelity.** A 1d signal's score includes a 0.20 `mtf_aligned` factor read from the next-higher timeframe. To reproduce live rows exactly, each replay step resamples the **prior** daily slice to 1wk/1mo, enriches them, and passes `{"1d","1wk","1mo"}` to `analyze_frames` — then filters results to `timeframe=="1d"`. This costs two small resamples + two `build_frame`s per step (cheap; higher frames are short).
- **D3 — Warmup window.** Skip the first `warmup_bars` daily steps (default **250**) so both the 1d EMAs and the resampled weekly EMA50 are warm. This is the honest usable window: ~250 daily steps/ticker over 2y, not the naive ~500. Early steps have shorter effective history than a live run would have had and must be discarded.
- **D4 — Reuse, never reimplement.** `advance_open`/`open_from_signals`/`_bar_row` are imported and run against a real `Session` over `sqlite:///:memory:`. A forked "pure twin" drifts and silently validates a different engine than production — explicitly forbidden.
- **D5 — Replay output is a SCREEN, not proof.** It inherits optimistic exact-level fills, survivorship (2y bars only for today's S&P members), next-bar-only fills, and gross-of-cost R. Every replay number is an **upper bound**. The guard's `margin_r` exists to absorb this; the optional slippage haircut (Task B6) and shuffle placebo (Task B7) quantify it.
- **D6 — Pairing key.** The arms are the SAME fills under different exit management, so the guard pairs by the natural key `(ticker, timeframe, opened_date, play_type)`. This is unique per fill within a run-date today. A future `fill_group_id` column (cross-links to the Step 5 `signal_id` fix from the roadmap) would make pairing exact; noted, not built here.

**Commit discipline:** commit after every green task. Run `ruff check . && mypy && pytest -q` before each commit (the repo's CI gate). Use `py -3.12` / the `.venv` per the project setup.

---

# PART A — Step 1: The significance / margin guard

## Task A1: Closed-trade loaders in `db/repo.py`

**Files:**
- Modify: `src/swing_screener/db/repo.py` (add after `load_open_paper_trades`, ~line 57)
- Test: `tests/db/test_repo_paper_trade_loaders.py` (create)

**Step 1 — Write the failing test:**

```python
# tests/db/test_repo_paper_trade_loaders.py
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import Base, PaperTrade


def _engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def _pt(**kw):
    base = dict(ticker="AMD", timeframe="1d", horizon="medium", play_type="continuation",
                signal_score=0.8, rank=1, fill_status="filled", stop=9.0, target=12.0,
                risk=1.0, status="closed", realized_r=1.5, arm="baseline")
    base.update(kw)
    return PaperTrade(**base)


def test_load_closed_paper_trades_filters_status_fill_and_optional_facets():
    eng = _engine()
    with Session(eng) as s:
        s.add_all([
            _pt(),                                            # closed+filled baseline
            _pt(arm="partial33_cond"),                        # closed+filled other arm
            _pt(status="open", realized_r=None),              # open -> excluded
            _pt(fill_status="missed", status="closed",        # not filled -> excluded
                realized_r=None),
            _pt(play_type="reversal", strength="early"),      # reversal
        ])
        s.commit()

        assert len(repo.load_closed_paper_trades(s)) == 3
        assert len(repo.load_closed_paper_trades(s, arm="baseline")) == 1
        assert len(repo.load_closed_paper_trades(s, play_type="reversal")) == 1
        assert len(repo.load_all_paper_trades(s)) == 5
```

**Step 2 — Run, verify it fails:** `pytest tests/db/test_repo_paper_trade_loaders.py -q` → FAIL (`AttributeError: ... load_closed_paper_trades`).

**Step 3 — Implement (in `repo.py`, after `load_open_paper_trades`):**

```python
def load_closed_paper_trades(
    session: Session, *, play_type: str | None = None, arm: str | None = None
) -> list[PaperTrade]:
    """Filled trades that have closed with a realized result. Optional play_type / arm
    facets for sliced A/B reads. Mirrors analytics.performance._is_closed_filled."""
    stmt = select(PaperTrade).where(
        PaperTrade.status == "closed", PaperTrade.fill_status == "filled"
    )
    if play_type is not None:
        stmt = stmt.where(PaperTrade.play_type == play_type)
    if arm is not None:
        stmt = stmt.where(PaperTrade.arm == arm)
    return list(session.scalars(stmt))


def load_all_paper_trades(session: Session) -> list[PaperTrade]:
    """Every paper trade (any status/fill). For replay aggregation + QC."""
    return list(session.scalars(select(PaperTrade)))
```

**Step 4 — Run, verify pass.** **Step 5 — Commit:** `feat(repo): closed/all paper-trade loaders for A/B analytics`.

---

## Task A2: The clustered paired-bootstrap guard

**Files:**
- Create: `src/swing_screener/analytics/significance.py`
- Test: `tests/analytics/test_significance.py`

**Step 1 — Write the failing tests** (math + gates + correction):

```python
# tests/analytics/test_significance.py
from swing_screener.analytics.significance import (
    ArmVerdict, compare_arm_to_baseline, evaluate_arms,
)
from swing_screener.db.models import PaperTrade


def _pt(ticker, opened_day, arm, realized_r, *, tf="1d", play="continuation"):
    from datetime import date
    return PaperTrade(
        ticker=ticker, timeframe=tf, horizon="medium", play_type=play,
        signal_score=0.8, rank=1, fill_status="filled", stop=9.0, target=12.0,
        risk=1.0, status="closed", realized_r=realized_r, arm=arm,
        opened_date=date(2026, 1, opened_day),
    )


def _book(diffs_by_ticker):
    """diffs_by_ticker: {ticker: [(baseline_r, arm_r), ...]} -> a paired book."""
    trades = []
    for tk, pairs in diffs_by_ticker.items():
        for i, (b, a) in enumerate(pairs, start=1):
            trades.append(_pt(tk, i, "baseline", b))
            trades.append(_pt(tk, i, "partial33_cond", a))
    return trades


def test_insufficient_data_when_below_min_pairs():
    v = compare_arm_to_baseline(_book({"AMD": [(1.0, 1.2)]}), "partial33_cond",
                                min_pairs=30, min_clusters=10)
    assert v.verdict == "insufficient_data"


def test_no_edge_when_difference_is_zero():
    # 40 pairs across 12 tickers, arm == baseline exactly -> diff 0, not a winner.
    book = {f"T{t}": [(1.0, 1.0)] * 4 for t in range(12)}
    v = compare_arm_to_baseline(_book(book), "partial33_cond",
                                min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    assert v.n_pairs == 48 and v.n_clusters == 12
    assert abs(v.mean_diff_r) < 1e-9
    assert v.verdict == "no_edge"


def test_winner_requires_ci_above_margin():
    # Large, consistent +0.5R edge on every pair across 15 tickers -> clears margin.
    book = {f"T{t}": [(0.0, 0.5)] * 4 for t in range(15)}
    v = compare_arm_to_baseline(_book(book), "partial33_cond",
                                min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    assert v.mean_diff_r > 0.45
    assert v.ci_low > 0.05
    assert v.verdict == "winner"


def test_small_edge_below_margin_is_no_edge():
    # +0.02R edge < 0.05R margin -> not a winner even if "positive".
    book = {f"T{t}": [(0.0, 0.02)] * 4 for t in range(15)}
    v = compare_arm_to_baseline(_book(book), "partial33_cond",
                                min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    assert v.verdict == "no_edge"


def test_multiple_comparisons_widens_the_bar():
    # Same data judged alone vs in a family of 3 -> the corrected CI is wider, so a
    # borderline winner can fall back to no_edge under correction.
    book = {f"T{t}": [(0.0, 0.10)] * 4 for t in range(15)}
    alone = compare_arm_to_baseline(_book(book), "partial33_cond", family_size=1,
                                    min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    corrected = compare_arm_to_baseline(_book(book), "partial33_cond", family_size=3,
                                        min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    assert corrected.ci_low <= alone.ci_low


def test_evaluate_arms_sets_family_size_to_challenger_count():
    book = {f"T{t}": [(0.0, 0.5)] * 4 for t in range(15)}
    trades = _book(book)
    # add a second challenger arm so the family has 2 members
    from datetime import date
    for t in range(15):
        for i in range(1, 5):
            trades.append(_pt(f"T{t}", i, "partial33_chand", 0.5))
    out = evaluate_arms(trades, ["partial33_cond", "partial33_chand"], seed=1,
                        min_pairs=30, min_clusters=10)
    assert set(out) == {"partial33_cond", "partial33_chand"}
    assert all(isinstance(v, ArmVerdict) and v.family_size == 2 for v in out.values())
```

**Step 2 — Run, verify fail** (`ModuleNotFoundError`).

**Step 3 — Implement `analytics/significance.py`:**

```python
"""Statistical guard for the parallel-arm A/B: is an arm's edge over baseline real,
or noise on a small, optimistically-filled sample?

The three live arms (baseline / partial33_cond / partial33_chand, see
pipeline/arms.py) are the SAME underlying fills under different exit management, so
the honest comparison is a PAIRED difference in realized_r, resampled by TICKER
(trades on one name are correlated -- treating each trade as independent overstates
significance). On top we demand a MARGIN (optimistic exact-level stop/target fills
inflate every arm's R) and a Bonferroni multiple-comparisons correction across the
family of non-baseline arms. Pure, no I/O.
"""

import random
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from swing_screener.db.models import PaperTrade

PairKey = tuple[str, str, object, str]  # (ticker, timeframe, opened_date, play_type)


def _is_closed_filled(t: PaperTrade) -> bool:
    return t.status == "closed" and t.fill_status == "filled" and t.realized_r is not None


def _pair_key(t: PaperTrade) -> PairKey:
    return (t.ticker, t.timeframe, t.opened_date, t.play_type)


@dataclass(frozen=True)
class ArmVerdict:
    arm: str
    baseline: str
    n_pairs: int
    n_clusters: int          # distinct tickers (the resampling unit)
    mean_diff_r: float       # arm expectancy_r - baseline expectancy_r (pooled)
    ci_low: float
    ci_high: float
    margin_r: float
    alpha: float
    family_size: int
    verdict: str             # "insufficient_data" | "no_edge" | "winner"
    detail: str


def _paired_diffs(trades: list[PaperTrade], arm: str, baseline: str
                  ) -> dict[str, list[float]]:
    """Map ticker -> list of (arm_r - baseline_r) for every fill present in BOTH arms."""
    by_arm: dict[str, dict[PairKey, float]] = defaultdict(dict)
    for t in trades:
        if t.arm in (arm, baseline) and _is_closed_filled(t):
            assert t.realized_r is not None
            by_arm[t.arm][_pair_key(t)] = t.realized_r
    base, chal = by_arm.get(baseline, {}), by_arm.get(arm, {})
    out: dict[str, list[float]] = defaultdict(list)
    for key in base.keys() & chal.keys():
        out[key[0]].append(chal[key] - base[key])  # key[0] == ticker
    return out


def compare_arm_to_baseline(
    trades: list[PaperTrade], arm: str, *, baseline: str = "baseline",
    min_pairs: int = 30, min_clusters: int = 10, margin_r: float = 0.05,
    alpha: float = 0.05, n_boot: int = 2000, family_size: int = 1, seed: int = 0,
) -> ArmVerdict:
    """Ticker-clustered paired bootstrap of the realized_r difference (arm - baseline).

    A "winner" requires: enough paired fills (min_pairs) across enough distinct tickers
    (min_clusters), AND the Bonferroni-corrected two-sided CI lower bound to clear the
    optimistic-fill margin (ci_low > margin_r). Otherwise "no_edge"; below the data
    floor, "insufficient_data".
    """
    diffs_by_ticker = _paired_diffs(trades, arm, baseline)
    all_diffs = [d for ds in diffs_by_ticker.values() for d in ds]
    n_pairs = len(all_diffs)
    tickers = list(diffs_by_ticker)
    n_clusters = len(tickers)
    mean_diff = float(np.mean(all_diffs)) if all_diffs else 0.0

    # Bonferroni: split alpha across the family, two-sided percentile CI.
    eff_alpha = alpha / max(family_size, 1)
    lo_pct, hi_pct = 100.0 * eff_alpha / 2.0, 100.0 * (1.0 - eff_alpha / 2.0)

    if n_pairs < min_pairs or n_clusters < min_clusters:
        return ArmVerdict(arm, baseline, n_pairs, n_clusters, mean_diff, float("nan"),
                          float("nan"), margin_r, alpha, family_size, "insufficient_data",
                          f"need >={min_pairs} pairs / >={min_clusters} tickers; "
                          f"have {n_pairs}/{n_clusters}")

    rng = random.Random(seed)
    boot_means: list[float] = []
    for _ in range(n_boot):
        chosen = [rng.choice(tickers) for _ in tickers]          # resample CLUSTERS
        sample = [d for tk in chosen for d in diffs_by_ticker[tk]]
        boot_means.append(sum(sample) / len(sample))
    ci_low = float(np.percentile(boot_means, lo_pct))
    ci_high = float(np.percentile(boot_means, hi_pct))

    verdict = "winner" if ci_low > margin_r else "no_edge"
    detail = (f"{arm} vs {baseline}: {mean_diff:+.3f}R "
              f"[{ci_low:+.3f}, {ci_high:+.3f}] over {n_pairs} pairs / {n_clusters} "
              f"tickers; margin {margin_r:.3f}R, family {family_size} -> {verdict}")
    return ArmVerdict(arm, baseline, n_pairs, n_clusters, mean_diff, ci_low, ci_high,
                      margin_r, alpha, family_size, verdict, detail)


def evaluate_arms(
    trades: list[PaperTrade], arms: list[str], *, baseline: str = "baseline", **kw: object
) -> dict[str, ArmVerdict]:
    """Compare each challenger arm to baseline with family_size = number of challengers
    (the multiple-comparisons family). kw passes through to compare_arm_to_baseline."""
    kw.pop("family_size", None)  # we own it here
    return {
        arm: compare_arm_to_baseline(trades, arm, baseline=baseline,
                                     family_size=len(arms), **kw)  # type: ignore[arg-type]
        for arm in arms
    }
```

**Step 4 — Run, verify pass.** Tweak `n_boot`/`seed` only if a percentile assertion is flaky (the engineered data is well-separated, so it should be stable).

**Step 5 — Commit:** `feat(analytics): ticker-clustered paired-bootstrap A/B significance guard`.

---

## Task A3: Wire the guard into the dashboard arm A/B view

**Files:**
- Modify: `src/swing_screener/dashboard/app.py` (the Performance / arm A/B section — **read it first** to find the existing `breakdown(..., "arm")` render; near the raw `select(PaperTrade)` ~line 419)
- Modify: `src/swing_screener/dashboard/ui.py` (add a pure formatter so the verdict is unit-testable without Streamlit)
- Test: `tests/dashboard/test_ui_arm_verdict.py`

**Step 1 — Write the failing test (pure formatter):**

```python
# tests/dashboard/test_ui_arm_verdict.py
from swing_screener.analytics.significance import ArmVerdict
from swing_screener.dashboard.ui import format_arm_verdict


def _v(verdict, **kw):
    base = dict(arm="partial33_cond", baseline="baseline", n_pairs=40, n_clusters=12,
                mean_diff_r=0.08, ci_low=0.06, ci_high=0.10, margin_r=0.05, alpha=0.05,
                family_size=2, verdict=verdict, detail="d")
    base.update(kw)
    return ArmVerdict(**base)


def test_winner_is_flagged_clearly():
    out = format_arm_verdict(_v("winner"))
    assert "✅" in out and "+0.08R" in out and "partial33_cond" in out


def test_insufficient_data_shows_counts_not_a_false_winner():
    out = format_arm_verdict(_v("insufficient_data", n_pairs=4, n_clusters=2,
                                ci_low=float("nan"), ci_high=float("nan")))
    assert "insufficient" in out.lower() and "4" in out
    assert "✅" not in out
```

**Step 2 — Run, verify fail.**

**Step 3 — Implement `format_arm_verdict` in `ui.py`:**

```python
def format_arm_verdict(v: "ArmVerdict") -> str:  # ArmVerdict imported under TYPE_CHECKING
    """One-line human verdict for the dashboard arm A/B table."""
    icon = {"winner": "✅", "no_edge": "➖", "insufficient_data": "⏳"}.get(v.verdict, "?")
    if v.verdict == "insufficient_data":
        return (f"{icon} {v.arm}: insufficient data "
                f"({v.n_pairs} pairs / {v.n_clusters} tickers)")
    return (f"{icon} {v.arm}: {v.mean_diff_r:+.2f}R "
            f"[{v.ci_low:+.2f}, {v.ci_high:+.2f}] vs {v.baseline} "
            f"· {v.n_pairs} pairs/{v.n_clusters} tickers · {v.verdict}")
```

(Add `from swing_screener.analytics.significance import ArmVerdict` under the existing `TYPE_CHECKING` block, or a plain import if `ui.py` has none.)

**Step 4 — Wire into `app.py`:** after the existing per-arm breakdown table, add an expander that loads closed trades and renders verdicts. Read the current section first; the wiring is approximately:

```python
from swing_screener.analytics.significance import evaluate_arms
from swing_screener.dashboard.ui import format_arm_verdict
from swing_screener.pipeline.arms import BASELINE, build_arms
from swing_screener.config import StrategyConfig

closed = repo.load_closed_paper_trades(session, play_type=play_type_filter)  # reuse existing filter
challengers = [a for a in build_arms(StrategyConfig()) if a != BASELINE]
verdicts = evaluate_arms(closed, challengers)
with st.expander("A/B significance (clustered paired bootstrap, optimistic-fill margin)"):
    st.caption("Replay-/forward-fills are optimistic; a 'winner' must clear the margin, "
               "not merely beat zero. Multiple-comparisons corrected across arms.")
    for arm in challengers:
        st.write(format_arm_verdict(verdicts[arm]))
```

**Step 5 — Smoke test** the app still imports/builds via the existing `AppTest` pattern (find the current dashboard smoke test and extend it, or assert `format_arm_verdict` is called with no exception on an empty book). Then **commit:** `feat(dashboard): A/B significance verdicts on the arm view`.

---

# PART B — Step 2: The 1d replay backtester

## Task B0: Read these before writing replay code

No code. Confirm by reading:
- [run.py:241-300](../../src/swing_screener/pipeline/run.py) — the night the replay reproduces: `prior_frames = f.iloc[:-1]` → `analyze_frames(prior)` + `analyze_reversals(prior)` → `open_from_signals(..., next_bars=today's high/low)` → `advance_open(..., latest_bars=_bar_row(full))`.
- [analyze.py:117-180](../../src/swing_screener/pipeline/analyze.py) — `analyze_frames` does **not** re-enrich (slicing a once-enriched frame is valid); `mtf_aligned` reads the next-higher provided frame's last bar.
- [shadow.py:55-131,134-270](../../src/swing_screener/pipeline/shadow.py) — `open_from_signals(session, candidates, next_bars, *, fill_date, arms)` and `advance_open(session, latest_bars, arms, *, today)`; the same-day guard skips a trade on its fill bar.
- [run.py:34,154-164](../../src/swing_screener/pipeline/run.py) — `_BAR_KEYS` and `_bar_row(frame)` (reused verbatim for `latest_bars`).

## Task B1: No-lookahead invariant test (write this FIRST — it is the whole point)

**Files:**
- Test: `tests/pipeline/test_replay_no_lookahead.py`
- (depends on `pipeline/replay.py` from B2 — write the test, watch it fail to import, then build B2)

**Step 1 — Write the test.** The property: the signal+fill decision at step `i` must not change when future bars are added or mutated. We assert that running the replay step at cursor `i` over `F[:k]` is identical for any `k > i`.

```python
# tests/pipeline/test_replay_no_lookahead.py
import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import replay_ticker
from tests.pipeline._replay_fixtures import synthetic_daily  # helper from B2


def test_future_bars_do_not_change_past_fills():
    cfg = StrategyConfig()
    full = synthetic_daily(n=400)            # deterministic engineered series
    truncated = full.iloc[:380]

    book_full = replay_ticker("SYN", full, cfg, warmup_bars=250, seed=0)
    book_trunc = replay_ticker("SYN", truncated, cfg, warmup_bars=250, seed=0)

    # Every trade OPENED on or before the truncation point must be identical
    # (same entry/stop/target/risk/realized_r/exit_reason) in both runs.
    def opened_by(book, cutoff):
        return {(t.ticker, t.timeframe, t.opened_date, t.arm): t
                for t in book if t.opened_date is not None and t.opened_date <= cutoff}

    cutoff = truncated.index[-2].date()      # last fully-decidable day in the short run
    a = opened_by(book_full, cutoff)
    b = opened_by(book_trunc, cutoff)
    shared = a.keys() & b.keys()
    assert shared, "expected overlapping fills to compare"
    for k in shared:
        assert a[k].entry_price == b[k].entry_price
        assert a[k].stop == b[k].stop and a[k].target == b[k].target
        # realized_r matches only where BOTH have closed by the cutoff data; compare
        # the booked partial/exit economics that were decided on/before cutoff.
        if a[k].exit_date and b[k].exit_date and a[k].exit_date <= cutoff:
            assert a[k].realized_r == b[k].realized_r
            assert a[k].exit_reason == b[k].exit_reason
```

**Step 2 — Run, verify fail** (`ModuleNotFoundError: replay` / missing fixture). Proceed to B2.

## Task B2: The replay driver `pipeline/replay.py`

**Files:**
- Create: `src/swing_screener/pipeline/replay.py`
- Create: `tests/pipeline/_replay_fixtures.py` (deterministic synthetic daily series)

**Step 1 — Write the fixture** (`tests/pipeline/_replay_fixtures.py`). A causal, no-randomness OHLCV series engineered to produce uptrends + pullbacks (continuation triggers) and a decline+bounce (reversal). Keep it deterministic (no `np.random`):

```python
import numpy as np
import pandas as pd


def synthetic_daily(n: int = 400) -> pd.DataFrame:
    """Deterministic daily OHLCV: a long uptrend with periodic shallow pullbacks
    (continuation triggers) and one deep decline+bounce (reversal trigger)."""
    idx = pd.bdate_range("2024-01-01", periods=n)
    t = np.arange(n)
    base = 50 + 0.05 * t                                  # gentle uptrend
    pullback = 1.5 * np.sin(t / 7.0)                       # rhythmic pullbacks
    dip = np.where((t > n // 2) & (t < n // 2 + 12), -6.0, 0.0)  # one sharp decline
    close = base + pullback + dip
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.4
    low = np.minimum(open_, close) - 0.4
    vol = np.full(n, 1_000_000.0)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": vol}, index=idx)
```

(After B2 runs, if no continuation/reversal fires, tune the constants until `replay_ticker` yields ≥1 of each — the test in B1 only needs *some* overlapping fills.)

**Step 2 — Implement `pipeline/replay.py`:**

```python
"""Deterministic offline replay of the live engine over historical daily bars.

For one ticker, enrich the daily frame ONCE (every indicator is causal
ewm(adjust=False), so F.iloc[:i] is a valid point-in-time slice -- the same property
the live nightly run relies on at run.py:241). Then slide a cursor i and, at each
step, reproduce exactly one live "night":

    prior daily = F.iloc[:i]            # ends "yesterday" (bar i-1) -- the detection bar
    fill bar    = F.iloc[i]             # "today" -- the bar we fill the prior signal on
    advance bar = _bar_row(F.iloc[:i+1])

The engine functions (analyze_frames/analyze_reversals/open_from_signals/advance_open
/_bar_row) are REUSED verbatim against a throwaway in-memory SQLite -- never a twin.
1d-only (D1); higher timeframes are rebuilt per step only to reproduce mtf_aligned
faithfully (D2), then results are filtered to 1d. Output is a SCREEN, not proof (D5).
"""

import logging
from datetime import date

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.data.resample import resample_ohlcv
from swing_screener.db import repo
from swing_screener.db.models import Base, PaperTrade
from swing_screener.pipeline.analyze import (
    analyze_frames, analyze_reversals, build_frames,
)
from swing_screener.pipeline.arms import build_arms
from swing_screener.pipeline.run import _bar_row
from swing_screener.pipeline.shadow import FillCandidate, advance_open, open_from_signals
from swing_screener.signals.frame import build_frame

log = logging.getLogger(__name__)


def _higher_frames(prior_raw: pd.DataFrame, cfg: StrategyConfig) -> dict[str, pd.DataFrame]:
    """Resample the PRIOR daily slice to 1wk/1mo and enrich -- so the mtf_aligned read
    for a 1d signal matches what a live run on that date would have computed. Built per
    step because a once-resampled weekly frame's last (partial) bar would leak future
    daily bars."""
    out: dict[str, pd.DataFrame] = {}
    wk = resample_ohlcv(prior_raw, "1W")
    mo = resample_ohlcv(prior_raw, "1ME")
    if len(wk):
        out["1wk"] = build_frame(wk, cfg)
    if len(mo):
        out["1mo"] = build_frame(mo, cfg)
    return out


def replay_ticker(
    ticker: str, daily_raw: pd.DataFrame, cfg: StrategyConfig, *,
    warmup_bars: int = 250, seed: int = 0,
) -> list[PaperTrade]:
    """Replay one ticker's 1d engine over daily_raw; return the resulting closed+open
    PaperTrade rows (detached from the throwaway session). Long-only, 1d-only."""
    if len(daily_raw) <= warmup_bars + 2:
        return []
    enriched = build_frame(daily_raw, cfg)          # enrich ONCE; slice thereafter
    arms = build_arms(cfg)
    arm_names = tuple(arms)

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        for i in range(warmup_bars, len(enriched)):
            today: date = enriched.index[i].date()
            prior_daily = enriched.iloc[:i]          # ends at bar i-1 ("yesterday")
            prior_raw = daily_raw.iloc[:i]
            frames = {"1d": prior_daily, **_higher_frames(prior_raw, cfg)}

            prior_signals = [
                r for r in (analyze_frames(ticker, frames, cfg)
                            + analyze_reversals(ticker, frames, cfg))
                if r.timeframe == "1d"               # D1: only fill 1d trades
            ]
            bar_i = enriched.iloc[i]
            next_bars = {(ticker, "1d"): (float(bar_i["high"]), float(bar_i["low"]))}
            # rank within play_type, mirroring run.py's prior-set ranking
            cont = sorted((r for r in prior_signals if r.play_type == "continuation"),
                          key=lambda r: r.score, reverse=True)
            rev = sorted((r for r in prior_signals if r.play_type == "reversal"),
                         key=lambda r: r.score, reverse=True)
            candidates = [
                FillCandidate(r.ticker, r.timeframe, r.horizon, r.score, rank,
                              r.mtf_aligned, None, r.zone, quality_tier=r.quality_tier,
                              volatility_tier=r.volatility_tier, oversold=r.oversold,
                              play_type=r.play_type, strength=r.strength)
                for group in (cont, rev)
                for rank, r in enumerate(group, start=1)
            ]
            open_from_signals(s, candidates, next_bars, fill_date=today, arms=arm_names)
            latest_bars = {(ticker, "1d"): _bar_row(enriched.iloc[:i + 1])}
            advance_open(s, latest_bars, arms, today=today)

        return [_detach(t) for t in repo.load_all_paper_trades(s)]


def _detach(t: PaperTrade) -> PaperTrade:
    """Copy the row's columns into a fresh, session-free PaperTrade for aggregation."""
    cols = {c.name: getattr(t, c.name) for c in PaperTrade.__table__.columns
            if c.name != "id"}
    return PaperTrade(**cols)
```

**Step 3 — Run B1 + a basic B2 smoke test:**

```python
# tests/pipeline/test_replay_basic.py
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import replay_ticker
from tests.pipeline._replay_fixtures import synthetic_daily


def test_replay_produces_a_book_with_both_play_types():
    book = replay_ticker("SYN", synthetic_daily(400), StrategyConfig(),
                         warmup_bars=250, seed=0)
    assert book, "expected some paper trades"
    plays = {t.play_type for t in book}
    assert "continuation" in plays
    # every filled+closed trade has a finite realized_r and positive risk
    for t in book:
        if t.status == "closed" and t.fill_status == "filled":
            assert t.realized_r is not None and t.risk and t.risk > 0
```

Run `pytest tests/pipeline/test_replay_basic.py tests/pipeline/test_replay_no_lookahead.py -q`. If the fixture produced no reversal, that's fine for B1; tune later if you want reversal coverage.

**Step 4 — verify both pass.** **Step 5 — Commit:** `feat(replay): deterministic 1d replay backtester reusing the live engine`.

## Task B3: Differential test — replay step ≡ a hand-built live-night

**Files:** `tests/pipeline/test_replay_matches_live_night.py`

Prove the replay's per-step orchestration matches calling the live functions directly the way `run.py` does, over the SAME two-bar slice — guarding the indexing (prior=`:i`, fill=`i`, advance=`:i+1`).

```python
# tests/pipeline/test_replay_matches_live_night.py
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import repo
from swing_screener.db.models import Base
from swing_screener.pipeline.analyze import analyze_frames, analyze_reversals
from swing_screener.pipeline.arms import build_arms
from swing_screener.pipeline.run import _bar_row
from swing_screener.pipeline.replay import _higher_frames
from swing_screener.pipeline.shadow import FillCandidate, advance_open, open_from_signals
from swing_screener.signals.frame import build_frame
from tests.pipeline._replay_fixtures import synthetic_daily


def test_one_replay_step_equals_a_direct_live_call():
    cfg = StrategyConfig()
    daily = synthetic_daily(400)
    enriched = build_frame(daily, cfg)
    i = 300
    today = enriched.index[i].date()

    # --- direct "live night" over the same slice ---
    frames = {"1d": enriched.iloc[:i], **_higher_frames(daily.iloc[:i], cfg)}
    sigs = [r for r in analyze_frames("SYN", frames, cfg)
            + analyze_reversals("SYN", frames, cfg) if r.timeframe == "1d"]
    bar_i = enriched.iloc[i]
    next_bars = {("SYN", "1d"): (float(bar_i["high"]), float(bar_i["low"]))}
    cands = [FillCandidate(r.ticker, r.timeframe, r.horizon, r.score, 1, r.mtf_aligned,
                           None, r.zone, quality_tier=r.quality_tier,
                           volatility_tier=r.volatility_tier, oversold=r.oversold,
                           play_type=r.play_type, strength=r.strength) for r in sigs]
    eng = create_engine("sqlite:///:memory:"); Base.metadata.create_all(eng)
    with Session(eng) as s:
        open_from_signals(s, cands, next_bars, fill_date=today, arms=tuple(build_arms(cfg)))
        advance_open(s, {("SYN", "1d"): _bar_row(enriched.iloc[:i + 1])},
                     build_arms(cfg), today=today)
        direct = {(t.ticker, t.arm, t.opened_date): (t.entry_price, t.stop, t.target)
                  for t in repo.load_all_paper_trades(s)}

    # Replay over [:i+1] must produce the SAME fills opened on `today`.
    from swing_screener.pipeline.replay import replay_ticker
    book = replay_ticker("SYN", daily.iloc[:i + 1], cfg, warmup_bars=i - 1)
    replayed = {(t.ticker, t.arm, t.opened_date): (t.entry_price, t.stop, t.target)
                for t in book if t.opened_date == today}
    for k, v in replayed.items():
        assert direct.get(k) == v
```

Run, verify pass, **commit:** `test(replay): differential check vs a direct live-night call`.

## Task B4: Universe replay + the report script

**Files:**
- Modify: `src/swing_screener/pipeline/replay.py` (add `replay_universe`)
- Create: `scripts/replay_report.py`

**Step 1 — Add `replay_universe`:**

```python
def replay_universe(
    bars_by_ticker: dict[str, pd.DataFrame], cfg: StrategyConfig, *,
    warmup_bars: int = 250, seed: int = 0,
) -> list[PaperTrade]:
    """Replay each ticker independently (books don't interact) and concatenate the
    resulting PaperTrade rows. One bad ticker is logged and skipped (per-ticker
    isolation, mirroring run.py)."""
    out: list[PaperTrade] = []
    for ticker, daily in bars_by_ticker.items():
        try:
            out.extend(replay_ticker(ticker, daily, cfg, warmup_bars=warmup_bars, seed=seed))
        except Exception:  # noqa: BLE001 -- isolation
            log.warning("replay failed for %s; skipping", ticker, exc_info=True)
    return out
```

**Step 2 — `scripts/replay_report.py`:** load the universe + cached 1d bars via `data.fetch.fetch_bars(ticker, "1d", cache_dir=..., period="2y")`, run `replay_universe`, then print `analytics.performance.summarize` + `breakdown(book, "arm")` + the **A/B guard** (`analytics.significance.evaluate_arms`) per `play_type`. Include flags: `--max-tickers`, `--warmup`, `--shuffle` (placebo, Task B7), `--slippage-atr` (haircut, Task B6). No test required (operator script), but keep all logic delegating to the tested pure functions. **Commit:** `feat(replay): universe replay + offline report script`.

## Task B5 (optional, recommended): out-of-sample era split

In `scripts/replay_report.py`, add `--dev-until YYYY-MM-DD`: partition closed trades into a development era (≤ date) and a frozen confirmation era (> date) by `opened_date`, and run the guard on each separately. **Any arm/threshold chosen on dev must be re-confirmed on the held-out era** — without this every replay decision is in-sample. This is the single highest-value validity add the reviewers flagged as missing. Pure slicing over the book; no new core code.

## Task B6 (optional): fill-pessimism haircut knob

A reviewer-flagged de-biasing win, **default-off so live behaviour is unchanged**:

- Add to `StrategyConfig`: `exit_slippage_atr: float = 0.0`.
- In `shadow.advance_open`, when booking a level exit, haircut against the trader: `stop` fills at `pt.stop - cfg.exit_slippage_atr * atr_val`, `target` at `pt.target - cfg.exit_slippage_atr * atr_val` (worse for a long). Guard on a finite positive `atr_val`.
- Test: a closed trade's `realized_r` shrinks by the expected ATR fraction when `exit_slippage_atr > 0`, and is byte-identical to today when `= 0.0`.
- Replay then reports **both** optimistic (0.0) and haircut expectancy, converting "must discount for optimism" from a footnote into a measured number.

This touches the label-generating core — write the test first, keep the default a strict no-op, and run the full suite. **Commit separately:** `feat(shadow): optional ATR fill-slippage haircut on level exits (default off)`.

## Task B7 (optional): label-shuffle placebo control

In `scripts/replay_report.py --shuffle`: permute `realized_r` across the closed book (seeded) and re-run the guard. If any arm still shows a "winner", the harness is leaking — the shuffle should collapse all edges to noise. ~15 lines; the cheapest overfitting smoke alarm. **Commit:** `feat(replay): shuffle-label placebo control in the report`.

---

## Definition of done

- `ruff check . && mypy && pytest -q` all green; CI green on the PR.
- Step 1 guard renders verdicts on the live dashboard arm A/B (insufficient-data is the expected verdict today — that is correct, not a bug).
- `python scripts/replay_report.py --max-tickers 50` runs end-to-end and prints per-arm expectancy with guard verdicts for the 1d book.
- The no-lookahead test (B1) and differential test (B3) pass — the two that prove replay isn't a silently-different engine.
- **Interpretation guardrail (put in the PR description):** replay expectancy is a survivorship-biased, optimistically-filled, gross-of-cost **upper bound** — a screen that must clear the MC-corrected margin by a haircut, never a tie. The correct outcome of the first pass is very likely "ship nothing," and that is success.

## Out of scope (deliberately deferred to later roadmap steps)

Non-destructive labels (regime/volume/recurrence), the `signal_id`/`fill_group_id` join fix, analyst-call persistence, purged+embargoed walk-forward CV, and the meta-label logistic — all gated on this substrate existing and proving an edge survives. See the roadmap in the prior analysis.

"""Tests for the deterministic tiered grader in ``pipeline.reflect``.

The North Star is "evidence over narrative": the grader stamps every verdict in
code (no LLM), and the multiple-comparisons correction must have TEETH -- a bucket
that would confirm at the naive 2.5% bound must come back ``hunch`` under the
family-wise correction. Tests #3 (MC teeth) and #4 (distinct-ticker floor) are the
load-bearing ones; they recompute both bounds to prove the discrimination is real.
"""

import statistics

from swing_screener.analytics.performance import (
    _CLUSTER_FLOOR,
    _clustered_ci_low,
    summarize,
)
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.reflect import (
    _ALPHA,
    _bucket_bound,
    _family_size,
    grade,
)


def _pt(ticker: str, r: float, *, dimension: str = "market_trend", bucket: str = "bull",
        score: float = 0.9) -> PaperTrade:
    """A closed-filled paper trade with the deciding attrs set; the rest are inert
    defaults so the detached object summarizes cleanly."""
    kwargs: dict[str, object] = {
        "ticker": ticker, "timeframe": "1d", "horizon": "medium", "signal_score": score,
        "rank": 1, "stop": 1.0, "target": 1.0, "risk": 1.0, "play_type": "continuation",
        "status": "closed", "fill_status": "filled", "realized_r": r,
        # default dimension attrs so a trade only lands in the bucket under test
        "market_trend": "bull", "volatility_tier": "med",
    }
    kwargs[dimension] = bucket
    return PaperTrade(**kwargs)  # type: ignore[arg-type]


def _spread_book(means: list[float], *, per: int = 3, dimension: str = "market_trend",
                 bucket: str = "bull", prefix: str = "T") -> list[PaperTrade]:
    """One ticker per entry in ``means``, each contributing ``per`` constant-R trades.
    Spreading the PER-TICKER means widens the clustered bootstrap (it resamples whole
    tickers), which is exactly the lever the MC-teeth test pulls."""
    trades: list[PaperTrade] = []
    for i, m in enumerate(means):
        for _ in range(per):
            trades.append(_pt(f"{prefix}{i}", m, dimension=dimension, bucket=bucket))
    return trades


def _find(verdicts: list, dimension: str, bucket: str):
    return next(v for v in verdicts if v.dimension == dimension and v.bucket == bucket)


# ---------------------------------------------------------------------------
# #6: family size and the corrected percentile are the pre-registered constants.
# ---------------------------------------------------------------------------
def test_family_size_and_corrected_lower_pct():
    # 2 (market_trend) + 3 (volatility_tier) + 5 (score bands) = 10.
    assert _family_size() == 10
    # The Bonferroni-corrected one-sided level -> lower percentile passed to the bootstrap.
    corrected_lower_pct = 100.0 * _ALPHA / _family_size()
    assert corrected_lower_pct == 0.5


# ---------------------------------------------------------------------------
# #1: a bucket that clears the corrected bound on the FORWARD book -> forward_confirmed.
# ---------------------------------------------------------------------------
def test_forward_confirmed_when_forward_book_clears_corrected_bound():
    # 10 tickers x 3 = 30 closed, all strongly positive -> corrected bound well above 0.
    forward = _spread_book([1.5] * 10, per=3)
    verdicts = grade("continuation", forward, [])
    v = _find(verdicts, "market_trend", "bull")
    assert v.tier == "forward_confirmed"
    assert v.source == "forward"
    assert v.n_clusters >= _CLUSTER_FLOOR
    assert v.ci_low > 0.0


# ---------------------------------------------------------------------------
# #2: forward FAILS (too few tickers) but REPLAY clears -> replay_screened.
# ---------------------------------------------------------------------------
def test_replay_screened_when_forward_thin_but_replay_clears():
    # Forward: a strong edge but only 4 distinct tickers (< the cluster floor) -> thin.
    forward = _spread_book([1.5, 1.6, 1.7, 1.8], per=6, prefix="F")  # 24 closed, 4 tickers
    # Replay: 10 tickers, deep and strongly positive -> clears the corrected bound.
    replay = _spread_book([1.5] * 10, per=3, prefix="R")
    verdicts = grade("continuation", forward, replay)
    v = _find(verdicts, "market_trend", "bull")
    assert v.tier == "replay_screened"
    assert v.source == "replay"
    assert v.n_clusters >= _CLUSTER_FLOOR


# ---------------------------------------------------------------------------
# #3: MC TEETH -- clears the naive 2.5% bound but NOT the Bonferroni alpha/K bound.
# Recompute both bounds in-test to prove the discrimination is genuine.
# ---------------------------------------------------------------------------
def test_mc_correction_has_teeth_naive_passes_corrected_fails():
    # Wide per-ticker means: overall edge is positive but the cluster bootstrap has a fat
    # left tail, so the 2.5% percentile is > 0 while the 0.5% percentile dips <= 0.
    means = [-1.0, -0.6, -0.2, 0.2, 0.6, 1.0, 1.4, 1.8, 2.2, 2.6]
    forward = _spread_book(means, per=3)

    closed = [t for t in forward if t.realized_r is not None]
    s = summarize(closed)
    by_ticker: dict[str, list[float]] = {}
    for t in closed:
        if t.realized_r is not None:
            by_ticker.setdefault(t.ticker, []).append(t.realized_r)

    # Naive one-sided bound at alpha=0.05 (uncorrected), clustered at 2.5%.
    z_naive = statistics.NormalDist().inv_cdf(1.0 - _ALPHA)
    iid_naive = s.expectancy_r - z_naive * s.expectancy_stderr
    naive_low, n_clusters, thin = _clustered_ci_low(by_ticker, iid_naive, lower_pct=2.5)

    # Corrected one-sided bound at alpha/K, clustered at 100*alpha/K %.
    alpha_c = _ALPHA / _family_size()
    z_corr = statistics.NormalDist().inv_cdf(1.0 - alpha_c)
    iid_corr = s.expectancy_r - z_corr * s.expectancy_stderr
    corr_low, _, _ = _clustered_ci_low(by_ticker, iid_corr, lower_pct=100.0 * alpha_c)

    # The data genuinely straddles the threshold (this is what gives the test teeth).
    assert not thin and n_clusters >= _CLUSTER_FLOOR
    assert naive_low > 0.0, naive_low      # would confirm at the naive level
    assert corr_low <= 0.0, corr_low       # but fails the family-wise correction

    # And the grader, using the corrected bound, returns hunch -- NOT confirmed.
    verdicts = grade("continuation", forward, [])
    v = _find(verdicts, "market_trend", "bull")
    assert v.tier == "hunch"
    assert v.source == "none"              # nothing was confirmed; forward only fills display
    assert v.n > 0                          # ...but the forward sample is carried for display
    # The grader's reported corrected bound matches what we recomputed.
    assert abs(v.ci_low - corr_low) < 1e-9


# ---------------------------------------------------------------------------
# #4: distinct-ticker FLOOR -- a huge edge on < _CLUSTER_FLOOR tickers stays a hunch.
# ---------------------------------------------------------------------------
def test_distinct_ticker_floor_keeps_thin_edge_a_hunch():
    # 5 distinct tickers (< 8), 30 closed, every trade +2R -- an enormous, consistent edge.
    forward = _spread_book([2.0] * 5, per=6)   # 5 tickers, 30 closed
    eff, n_closed, n_clusters, exp, thin = _bucket_bound(forward, _family_size())
    assert n_clusters < _CLUSTER_FLOOR
    assert thin                                # bootstrap couldn't run -> IID fallback, thin
    assert n_closed >= 20                       # depth alone is not enough

    verdicts = grade("continuation", forward, [])
    v = _find(verdicts, "market_trend", "bull")
    assert v.tier == "hunch"                    # never confirmed/screened while thin


# ---------------------------------------------------------------------------
# #5: determinism -- identical inputs -> identical verdicts (no LLM, seeded bootstrap).
# ---------------------------------------------------------------------------
def test_grade_is_deterministic():
    forward = _spread_book([1.5] * 10, per=3)
    replay = _spread_book([-1.0, -0.6, -0.2, 0.2, 0.6, 1.0, 1.4, 1.8, 2.2, 2.6], per=3,
                          prefix="R")
    a = grade("continuation", forward, replay)
    b = grade("continuation", forward, replay)
    assert a == b


# ---------------------------------------------------------------------------
# Coverage: every (dimension, bucket) is emitted, including empty buckets as hunches.
# ---------------------------------------------------------------------------
def test_emits_one_verdict_per_bucket_including_empties():
    verdicts = grade("continuation", [], [])
    keys = {(v.dimension, v.bucket) for v in verdicts}
    assert ("market_trend", "bull") in keys
    assert ("market_trend", "bear") in keys
    assert ("volatility_tier", "low") in keys
    assert ("volatility_tier", "med") in keys
    assert ("volatility_tier", "high") in keys
    # score bands keyed off score_bucket's labels
    assert ("score", "0.70-0.80") in keys
    assert ("score", "0.80-1.00") in keys
    assert len(verdicts) == _family_size()
    # all empty -> all hunches with n == 0, source "none"
    for v in verdicts:
        assert v.tier == "hunch" and v.n == 0 and v.source == "none"


def test_score_band_bucket_grades_off_score_bucket_labels():
    # Trades with score in [0.80, 1.00) should land in the "0.80-1.00" band and,
    # with a deep consistent edge, confirm there.
    forward = _spread_book([1.5] * 10, per=3)  # score defaults to 0.9 -> "0.80-1.00" band
    verdicts = grade("continuation", forward, [])
    v = _find(verdicts, "score", "0.80-1.00")
    assert v.tier == "forward_confirmed"
    # a band with no trades is still emitted as a hunch
    empty_band = _find(verdicts, "score", "0.00-0.50")
    assert empty_band.tier == "hunch" and empty_band.n == 0

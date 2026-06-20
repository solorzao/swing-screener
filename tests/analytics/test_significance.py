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
    book = {f"T{t}": [(1.0, 1.0)] * 4 for t in range(12)}
    v = compare_arm_to_baseline(_book(book), "partial33_cond",
                                min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    assert v.n_pairs == 48 and v.n_clusters == 12
    assert abs(v.mean_diff_r) < 1e-9
    assert v.verdict == "no_edge"


def test_winner_requires_ci_above_margin():
    book = {f"T{t}": [(0.0, 0.5)] * 4 for t in range(15)}
    v = compare_arm_to_baseline(_book(book), "partial33_cond",
                                min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    assert v.mean_diff_r > 0.45
    assert v.ci_low > 0.05
    assert v.verdict == "winner"


def test_small_edge_below_margin_is_no_edge():
    book = {f"T{t}": [(0.0, 0.02)] * 4 for t in range(15)}
    v = compare_arm_to_baseline(_book(book), "partial33_cond",
                                min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    assert v.verdict == "no_edge"


def test_multiple_comparisons_widens_the_bar():
    # Heterogeneous diffs ACROSS tickers so cluster-resampling actually moves the sample
    # mean; only then can the Bonferroni split widen the interval. A book where every
    # ticker shares the same diff distribution is degenerate at the cluster level (every
    # cluster mean is identical, so resampling clusters never moves the mean and both CIs
    # collapse to a point) -- that would pass by equality without exercising the
    # correction. Cycling distinct per-ticker diffs gives genuine cluster spread.
    vals = (0.0, 0.05, 0.10, 0.25)
    book = {f"T{t}": [(0.0, vals[t % 4])] * 4 for t in range(15)}
    alone = compare_arm_to_baseline(_book(book), "partial33_cond", family_size=1,
                                    min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    corrected = compare_arm_to_baseline(_book(book), "partial33_cond", family_size=3,
                                        min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    assert corrected.ci_low < alone.ci_low      # strict: the bar genuinely rises
    assert corrected.ci_high > alone.ci_high     # and the upper end genuinely widens


def test_bootstrap_resamples_by_ticker_not_by_trade():
    # One heavy ticker carries many large-positive diffs; many light tickers sit near
    # zero. The pooled mean is dragged UP by the heavy ticker's many fills. But the
    # bootstrap resamples whole TICKERS (clusters) with replacement, so a sizable
    # fraction of resamples omit the heavy ticker entirely and collapse to ~0 -- that
    # drives ci_low far below the pooled mean. A per-TRADE bootstrap could not do this:
    # the heavy ticker's many fills would be represented in essentially every resample,
    # keeping ci_low near the pooled mean (verified by simulation: clustered ci_low=0.0
    # vs per-trade ci_low~=0.125 against a pooled mean of 0.20). So this assertion is a
    # genuine discriminator for the clustering design, not a vacuous pass.
    heavy = [(0.0, r) for r in (0.40, 0.45, 0.50, 0.55, 0.60, 0.42, 0.48, 0.52, 0.58,
                                0.44, 0.46, 0.54, 0.56, 0.41, 0.49)]
    light = [(0.0, -0.01), (0.0, 0.01)]
    book = {"HEAVY": heavy, **{f"L{t}": light for t in range(11)}}
    full = compare_arm_to_baseline(_book(book), "partial33_cond",
                                   min_pairs=30, min_clusters=10, margin_r=0.05, seed=7)
    assert full.n_clusters == 12 and full.mean_diff_r > 0.15  # heavy ticker dominates pool
    # whole-ticker resampling sometimes omits HEAVY -> ci_low collapses well below the mean
    assert full.ci_low < 0.5 * full.mean_diff_r
    assert full.ci_high > full.ci_low                          # non-degenerate interval

    # Dropping the heavy ticker materially changes the result (the pool was the heavy
    # ticker) -- only whole-ticker resampling is this sensitive to a single cluster.
    dropped = [t for t in _book(book) if t.ticker != "HEAVY"]
    d = compare_arm_to_baseline(dropped, "partial33_cond",
                                min_pairs=10, min_clusters=10, margin_r=0.05, seed=7)
    assert abs(d.mean_diff_r) < 1e-9 and full.mean_diff_r - d.mean_diff_r > 0.15


def test_pairing_key_discriminates_and_drops_unmatched():
    # Two fills on the SAME ticker+opened_date but different play_type must be TWO pairs.
    trades = []
    for t in range(15):
        tk = f"T{t}"
        trades.append(_pt(tk, 1, "baseline", 0.0, play="continuation"))
        trades.append(_pt(tk, 1, "partial33_cond", 0.5, play="continuation"))
        trades.append(_pt(tk, 1, "baseline", 0.0, play="reversal"))
        trades.append(_pt(tk, 1, "partial33_cond", 0.5, play="reversal"))
    # 15 tickers x 2 distinct play_types = 30 pairs (would be 15 if play_type collapsed).
    v = compare_arm_to_baseline(trades, "partial33_cond",
                                min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    assert v.n_pairs == 30 and v.n_clusters == 15

    # Unmatched fills (baseline with no challenger, and vice versa) must be DROPPED.
    unmatched = list(trades)
    # baseline-only fill: no partial33_cond partner on this (ticker, date, play).
    unmatched.append(_pt("T0", 9, "baseline", 0.0, play="continuation"))
    # challenger-only fill: no baseline partner on this (ticker, date, play).
    unmatched.append(_pt("T1", 9, "partial33_cond", 0.5, play="continuation"))
    v2 = compare_arm_to_baseline(unmatched, "partial33_cond",
                                 min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    assert v2.n_pairs == 30  # the two extra unmatched fills are not counted


def test_evaluate_arms_sets_family_size_to_challenger_count():
    book = {f"T{t}": [(0.0, 0.5)] * 4 for t in range(15)}
    trades = _book(book)
    for t in range(15):
        for i in range(1, 5):
            trades.append(_pt(f"T{t}", i, "partial33_chand", 0.5))
    out = evaluate_arms(trades, ["partial33_cond", "partial33_chand"], seed=1,
                        min_pairs=30, min_clusters=10)
    assert set(out) == {"partial33_cond", "partial33_chand"}
    assert all(isinstance(v, ArmVerdict) and v.family_size == 2 for v in out.values())

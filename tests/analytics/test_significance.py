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
    book = {f"T{t}": [(0.0, 0.10)] * 4 for t in range(15)}
    alone = compare_arm_to_baseline(_book(book), "partial33_cond", family_size=1,
                                    min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    corrected = compare_arm_to_baseline(_book(book), "partial33_cond", family_size=3,
                                        min_pairs=30, min_clusters=10, margin_r=0.05, seed=1)
    assert corrected.ci_low <= alone.ci_low


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

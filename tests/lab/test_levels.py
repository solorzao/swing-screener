"""Deterministic level math: swing pivots (strict-inequality rule shared with
entry_zone.nearest_resistance), greedy clustering, the S/R split by last close,
and the Fibonacci retracement's direction rule."""

from swing_screener.lab.levels import (
    FIB_RATIOS,
    cluster_levels,
    fib_retracement,
    pivot_highs,
    pivot_lows,
    sr_levels,
)


def test_pivot_highs_strict_both_sides() -> None:
    highs = [1.0, 2.0, 5.0, 2.0, 1.0, 3.0, 3.0, 3.0, 1.0]
    # 5.0 tops width=2 on both sides; the flat 3.0 run is deliberately NOT a pivot.
    assert pivot_highs(highs, 2) == [5.0]


def test_pivot_lows_mirror() -> None:
    lows = [5.0, 4.0, 1.0, 4.0, 5.0, 3.0, 3.0]
    assert pivot_lows(lows, 2) == [1.0]


def test_cluster_merges_within_tolerance() -> None:
    levels = cluster_levels([100.0, 100.3, 100.6, 110.0], tol_frac=0.005)
    assert len(levels) == 2
    assert levels[0].touches == 3
    assert abs(levels[0].price - 100.3) < 0.2
    assert levels[1].touches == 1 and levels[1].price == 110.0


def test_cluster_empty() -> None:
    assert cluster_levels([], tol_frac=0.005) == []


def test_sr_split_by_last_close_nearest_first() -> None:
    # Pivots at 90 (low), 95 (low), 105 (high), 112 (high); close at 100.
    highs = [100, 101, 105, 101, 100, 108, 112, 108, 100]
    lows = [92, 91, 90, 91, 96, 95.5, 95, 95.5, 96]
    out = sr_levels([float(x) for x in highs], [float(x) for x in lows], 100.0,
                    width=2, tol_frac=0.005)
    res = [lv.price for lv in out["resistance"]]
    sup = [lv.price for lv in out["support"]]
    assert res == sorted(res)                  # nearest resistance first
    assert sup == sorted(sup, reverse=True)    # nearest support first
    assert all(p > 100.0 for p in res)
    assert all(p <= 100.0 for p in sup)
    assert 105.0 in res and 112.0 in res
    assert 90.0 in sup and 95.0 in sup


def test_sr_caps_per_side() -> None:
    highs = [float(100 + (i % 10)) for i in range(80)]
    lows = [h - 2 for h in highs]
    out = sr_levels(highs, lows, 104.0, width=2, tol_frac=0.0001, max_per_side=3)
    assert len(out["resistance"]) <= 3 and len(out["support"]) <= 3


def test_fib_up_move() -> None:
    # Low early, high late -> an up-move being retraced; 0% sits AT the high.
    highs = [10.0, 11.0, 12.0, 15.0, 20.0]
    lows = [9.0, 10.0, 11.0, 14.0, 19.0]
    fib = fib_retracement(highs, lows)
    assert fib is not None
    assert fib["direction"] == "up"
    assert fib["high"] == 20.0 and fib["low"] == 9.0
    by_ratio = {lv["ratio"]: lv["price"] for lv in fib["levels"]}  # type: ignore[index]
    assert by_ratio[0.0] == 20.0 and by_ratio[1.0] == 9.0
    assert abs(by_ratio[0.5] - 14.5) < 1e-9
    assert set(by_ratio) == set(FIB_RATIOS)


def test_fib_down_move() -> None:
    # High early, low late -> a down-move; 0% sits AT the low.
    highs = [20.0, 19.0, 15.0, 12.0, 11.0]
    lows = [18.0, 17.0, 13.0, 10.0, 9.0]
    fib = fib_retracement(highs, lows)
    assert fib is not None
    assert fib["direction"] == "down"
    by_ratio = {lv["ratio"]: lv["price"] for lv in fib["levels"]}  # type: ignore[index]
    assert by_ratio[0.0] == 9.0 and by_ratio[1.0] == 20.0


def test_fib_degenerate_window_is_none() -> None:
    assert fib_retracement([5.0, 5.0], [5.0, 5.0]) is None
    assert fib_retracement([], []) is None

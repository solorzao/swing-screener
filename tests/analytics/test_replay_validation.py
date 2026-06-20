from datetime import date

from swing_screener.analytics.replay_validation import (
    partition_by_era, shuffle_realized_r,
)
from swing_screener.analytics.significance import compare_arm_to_baseline
from swing_screener.db.models import PaperTrade


def _pt(ticker, opened_day, arm, realized_r, *, tf="1d", play="continuation",
        status="closed", fill_status="filled"):
    """A closed, filled paper-trade row, opened on 2026-01-<opened_day>."""
    return PaperTrade(
        ticker=ticker, timeframe=tf, horizon="medium", play_type=play,
        signal_score=0.8, rank=1, fill_status=fill_status, stop=9.0, target=12.0,
        risk=1.0, status=status, realized_r=realized_r, arm=arm,
        opened_date=None if opened_day is None else date(2026, 1, opened_day),
    )


# --------------------------------------------------------------------------- #
# partition_by_era
# --------------------------------------------------------------------------- #

def test_partition_splits_at_cutoff():
    cutoff = date(2026, 1, 15)
    before = _pt("AMD", 10, "baseline", 1.0)
    boundary = _pt("AMD", 15, "baseline", 1.0)   # == cutoff -> dev
    after = _pt("AMD", 20, "baseline", 1.0)
    split = partition_by_era([before, boundary, after], cutoff)
    assert split.dev == [before, boundary]       # <= cutoff (boundary lands in dev)
    assert split.confirm == [after]              # > cutoff


def test_partition_drops_unfilled_none_opened_date():
    cutoff = date(2026, 1, 15)
    filled = _pt("AMD", 10, "baseline", 1.0)
    never_filled = _pt("AMD", None, "baseline", None, status="open",
                       fill_status="missed")
    split = partition_by_era([filled, never_filled], cutoff)
    assert split.dev == [filled]
    assert split.confirm == []
    # the None-opened_date row appears in neither era.
    assert never_filled not in split.dev and never_filled not in split.confirm


def test_partition_empty_input():
    split = partition_by_era([], date(2026, 1, 15))
    assert split.dev == [] and split.confirm == []


# --------------------------------------------------------------------------- #
# shuffle_realized_r
# --------------------------------------------------------------------------- #

def _closed_book(n_tickers=15, per_ticker=4):
    """A paired baseline/challenger book with a real challenger edge of +0.5R."""
    trades: list[PaperTrade] = []
    for t in range(n_tickers):
        tk = f"T{t}"
        for i in range(1, per_ticker + 1):
            trades.append(_pt(tk, i, "baseline", 0.0))
            trades.append(_pt(tk, i, "partial33_cond", 0.5))
    return trades


def test_shuffle_preserves_the_multiset_of_realized_r():
    book = _closed_book()
    before = sorted(t.realized_r for t in book)
    out = shuffle_realized_r(book, seed=0)
    after = sorted(t.realized_r for t in out)
    assert before == after                       # same values, just permuted


def test_shuffle_is_deterministic_for_a_fixed_seed():
    book = _closed_book()
    a = [t.realized_r for t in shuffle_realized_r(book, seed=7)]
    b = [t.realized_r for t in shuffle_realized_r(book, seed=7)]
    assert a == b


def test_shuffle_differs_for_different_seeds():
    book = _closed_book()        # 120 closed rows -- large enough that two seeds diverge
    a = [t.realized_r for t in shuffle_realized_r(book, seed=1)]
    b = [t.realized_r for t in shuffle_realized_r(book, seed=2)]
    assert a != b


def test_shuffle_passes_open_unfilled_rows_through_unchanged():
    closed = _pt("AMD", 1, "baseline", 1.0)
    open_row = _pt("AMD", None, "baseline", None, status="open", fill_status="missed")
    out = shuffle_realized_r([closed, open_row], seed=0)
    # open/unfilled row's realized_r stays None and is not part of the permutation.
    open_out = next(t for t in out if t.status == "open")
    assert open_out.realized_r is None


def test_shuffle_returns_copies_not_aliases():
    book = _closed_book(n_tickers=3, per_ticker=2)
    out = shuffle_realized_r(book, seed=0)
    assert all(o is not i for o, i in zip(out, book, strict=True))
    # mutating a copy must not touch the input.
    out[0].realized_r = 999.0
    assert all(t.realized_r != 999.0 for t in book)


def test_shuffle_collapses_a_real_edge_placebo():
    # A book engineered so the challenger arm has a clear +0.5R edge per pair.
    book = _closed_book(n_tickers=15, per_ticker=4)

    real = compare_arm_to_baseline(book, "partial33_cond", min_pairs=30,
                                   min_clusters=10, margin_r=0.05, seed=1)
    assert real.verdict == "winner"
    assert real.mean_diff_r > 0.45               # the real edge is large and positive

    # Re-run the guard on shuffled labels. The permutation scrambles which row carries
    # which realized_r, so the per-arm split that produced the edge dissolves: the
    # shuffled paired mean difference must be MUCH smaller in magnitude than the real
    # +0.5R. Deterministic with a fixed seed.
    shuffled = shuffle_realized_r(book, seed=123)
    placebo = compare_arm_to_baseline(shuffled, "partial33_cond", min_pairs=30,
                                      min_clusters=10, margin_r=0.05, seed=1)
    assert abs(placebo.mean_diff_r) < 0.5 * abs(real.mean_diff_r)
    # and the placebo no longer clears the margin -> not a winner (harness isn't leaking).
    assert placebo.verdict != "winner"

"""Task 4 test contract: day-of-week + hold-time breakdowns.

Every value is a full clustered-CI ``PerformanceSummary`` (reusing ``summarize``),
every bucket is present even when empty, and ``by_symbol`` is proven to be exactly
``breakdown(trades, "ticker")`` -- a thin re-export, not a reimplementation.
"""

from datetime import date

from swing_screener.analytics.performance import (
    PerformanceSummary,
    breakdown,
    summarize,
)
from swing_screener.db.models import PaperTrade
from swing_screener.journal.breakdowns import (
    by_day_of_week,
    by_hold_time,
    by_symbol,
)


def _pt(*, ticker="AAPL", realized_r=None, hold_bars=None, exit_date=None,
        fill_status="filled", status="closed"):
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        mtf_aligned=True, quality_tier="reputable", volatility_tier="med",
        fill_status=fill_status, stop=95.0, target=110.0, risk=5.0, status=status,
        realized_r=realized_r, hold_bars=hold_bars, exit_date=exit_date,
    )


# --- by_day_of_week ---------------------------------------------------------

def test_by_day_of_week_labels_grouping_and_golden_values():
    trades = [
        _pt(realized_r=2.0, exit_date=date(2024, 1, 1)),   # Monday
        _pt(realized_r=1.0, exit_date=date(2024, 1, 8)),   # Monday
        _pt(realized_r=-1.0, exit_date=date(2024, 1, 3)),  # Wednesday
    ]
    by = by_day_of_week(trades)
    # every weekday present, Monday-first, even the empty ones
    assert list(by) == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert all(isinstance(v, PerformanceSummary) for v in by.values())
    assert by["Mon"].n_closed == 2
    assert abs(by["Mon"].expectancy_r - 1.5) < 1e-9        # (2.0 + 1.0) / 2
    assert by["Wed"].n_closed == 1
    assert abs(by["Wed"].expectancy_r + 1.0) < 1e-9
    assert by["Tue"].n_closed == 0
    assert by["Thu"].n_closed == 0
    assert by["Fri"].n_closed == 0


def test_by_day_of_week_reuses_summarize():
    mondays = [
        _pt(realized_r=2.0, exit_date=date(2024, 1, 1)),
        _pt(realized_r=1.0, exit_date=date(2024, 1, 8)),
    ]
    by = by_day_of_week(mondays)
    # value-identical to summarizing the group directly -> reuse, not a reimplementation
    assert by["Mon"] == summarize(mondays)


def test_by_day_of_week_skips_rows_without_exit_date():
    # an open filled row has no exit_date -> it belongs to no weekday
    trades = [_pt(realized_r=None, status="open", hold_bars=2)]
    by = by_day_of_week(trades)
    assert list(by) == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert all(s.n_closed == 0 for s in by.values())


# --- by_hold_time -----------------------------------------------------------

def test_by_hold_time_default_edges_and_golden_values():
    trades = [
        _pt(realized_r=1.0, hold_bars=1),    # 1-1
        _pt(realized_r=0.5, hold_bars=2),    # 2-3
        _pt(realized_r=-0.5, hold_bars=3),   # 2-3
        _pt(realized_r=2.0, hold_bars=5),    # 4-5
        _pt(realized_r=-1.0, hold_bars=10),  # 6-10
        _pt(realized_r=3.0, hold_bars=15),   # 11+
    ]
    by = by_hold_time(trades)
    assert list(by) == ["1-1", "2-3", "4-5", "6-10", "11+"]
    assert by["1-1"].n_closed == 1
    assert by["2-3"].n_closed == 2
    assert abs(by["2-3"].expectancy_r - 0.0) < 1e-9        # (0.5 + -0.5) / 2
    assert by["4-5"].n_closed == 1
    assert by["6-10"].n_closed == 1
    assert by["11+"].n_closed == 1


def test_by_hold_time_every_bucket_present_when_empty():
    by = by_hold_time([])
    assert list(by) == ["1-1", "2-3", "4-5", "6-10", "11+"]
    assert all(isinstance(v, PerformanceSummary) for v in by.values())
    assert all(s.n_closed == 0 for s in by.values())


def test_by_hold_time_skips_none_hold_bars():
    # a closed row with no recorded hold cannot be placed on the hold axis
    by = by_hold_time([_pt(realized_r=1.0, hold_bars=None)])
    assert all(s.n_closed == 0 for s in by.values())


def test_by_hold_time_custom_edges():
    trades = [
        _pt(realized_r=1.0, hold_bars=2),   # 1-2
        _pt(realized_r=2.0, hold_bars=3),   # 3+
    ]
    by = by_hold_time(trades, edges=(2,))
    assert list(by) == ["1-2", "3+"]
    assert by["1-2"].n_closed == 1
    assert by["3+"].n_closed == 1


# --- by_symbol --------------------------------------------------------------

def test_by_symbol_is_breakdown_by_ticker():
    trades = [
        _pt(ticker="AAPL", realized_r=2.0, hold_bars=5, exit_date=date(2024, 1, 5)),
        _pt(ticker="AAPL", realized_r=-1.0, hold_bars=3, exit_date=date(2024, 1, 3)),
        _pt(ticker="MSFT", realized_r=1.0, hold_bars=4, exit_date=date(2024, 1, 8)),
    ]
    by = by_symbol(trades)
    assert set(by) == {"AAPL", "MSFT"}
    # thin re-export: value-identical to the analytics ticker breakdown
    assert by == breakdown(trades, "ticker")
    assert by["AAPL"].n_closed == 2
    assert by["MSFT"].n_closed == 1

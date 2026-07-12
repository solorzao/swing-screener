from datetime import date

import pytest

from swing_screener.journal.calendar import pnl_calendar


def _pt(*, realized_r=None, exit_date=None, fill_status="filled", status="closed",
        opened_date=None, partial_done=False):
    from swing_screener.db.models import PaperTrade
    return PaperTrade(
        ticker="AAPL", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        mtf_aligned=True, quality_tier="reputable", volatility_tier="med",
        fill_status=fill_status, stop=95.0, target=110.0, risk=5.0, status=status,
        realized_r=realized_r, exit_date=exit_date, opened_date=opened_date,
        partial_done=partial_done,
    )


def _golden_book():
    # Two days in June (2026-06-15 x2, 2026-06-16 x1) plus one day in July.
    return [
        _pt(realized_r=2.0, exit_date=date(2026, 6, 15)),
        _pt(realized_r=1.0, exit_date=date(2026, 6, 15)),
        _pt(realized_r=-1.0, exit_date=date(2026, 6, 16)),
        _pt(realized_r=3.0, exit_date=date(2026, 7, 3)),
        # noise: never counted
        _pt(fill_status="missed", realized_r=None, exit_date=date(2026, 6, 15)),
        _pt(status="open", realized_r=None, exit_date=None),
    ]


def test_pnl_calendar_golden_days_and_months():
    cal = pnl_calendar(_golden_book(), month=None)

    days = cal["days"]
    assert set(days) == {"2026-06-15", "2026-06-16", "2026-07-03"}
    assert days["2026-06-15"] == {"r": pytest.approx(3.0), "n": 2}
    assert days["2026-06-16"] == {"r": pytest.approx(-1.0), "n": 1}
    assert days["2026-07-03"] == {"r": pytest.approx(3.0), "n": 1}

    months = cal["months"]
    assert set(months) == {"2026-06", "2026-07"}
    assert months["2026-06"] == {"r": pytest.approx(2.0), "n": 3}
    assert months["2026-07"] == {"r": pytest.approx(3.0), "n": 1}


def test_pnl_calendar_cost_level_mixed_is_none():
    # June exits pre-date the 2026-07-02 cost cutoff, so the cohort is unprovable.
    cal = pnl_calendar(_golden_book(), month=None)
    assert cal["cost_level"] is None


def test_pnl_calendar_cost_level_net_when_all_post_cutoff():
    trades = [
        _pt(realized_r=1.5, exit_date=date(2026, 7, 3)),
        _pt(realized_r=-0.5, exit_date=date(2026, 7, 6)),
    ]
    cal = pnl_calendar(trades, month=None)
    assert cal["cost_level"] == "0.05"


def test_pnl_calendar_month_filters_days_but_months_span_all():
    cal = pnl_calendar(_golden_book(), month=date(2026, 7, 1))
    # days restricted to the selected calendar month (day component ignored)
    assert set(cal["days"]) == {"2026-07-03"}
    assert cal["days"]["2026-07-03"] == {"r": pytest.approx(3.0), "n": 1}
    # the month navigation strip still spans every month in the cohort
    assert set(cal["months"]) == {"2026-06", "2026-07"}
    assert cal["months"]["2026-06"] == {"r": pytest.approx(2.0), "n": 3}


def test_pnl_calendar_default_month_is_none():
    # callable without the keyword: aggregates every day
    cal = pnl_calendar(_golden_book())
    assert set(cal["days"]) == {"2026-06-15", "2026-06-16", "2026-07-03"}


def test_pnl_calendar_skips_closed_filled_without_exit_date():
    # a closed-filled result with no exit_date can't be keyed by day/month -> excluded
    trades = [
        _pt(realized_r=2.0, exit_date=None),
        _pt(realized_r=1.0, exit_date=date(2026, 7, 3)),
    ]
    cal = pnl_calendar(trades, month=None)
    assert set(cal["days"]) == {"2026-07-03"}
    assert set(cal["months"]) == {"2026-07"}
    assert cal["months"]["2026-07"] == {"r": pytest.approx(1.0), "n": 1}


def test_pnl_calendar_empty():
    cal = pnl_calendar([], month=None)
    assert cal == {"days": {}, "months": {}, "cost_level": None}

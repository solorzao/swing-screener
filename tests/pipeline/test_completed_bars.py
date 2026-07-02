"""Completed-bar semantics for the shadow book (2026-07 audit fixes).

Calendar timeframes (1wk / 1mo) resample with the IN-PROGRESS bucket as the frame's
last bar. The shadow book must never read that partial bar: open trades advance only
on completed bars of their own timeframe, and prior-bar signals book only when their
next bar has completed (period end), so a weekly fill window is the full next week --
not the first day of it -- and a setup is booked once, not once per daily run.
"""

from datetime import date

import pandas as pd

from swing_screener.pipeline.run import _is_period_end, _latest_completed_bar


def _weekly_frame(labels):
    """A minimal enriched weekly frame with the columns _bar_row reads."""
    n = len(labels)
    return pd.DataFrame(
        {
            "low": [99.0] * n, "high": [103.0] * n, "close": [100.0] * n,
            "shaved_head": [False] * n, "bearish": [False] * n,
            "shaved_bottom": [False] * n, "atr": [2.0] * n,
            "body_frac": [0.5] * n,
        },
        index=pd.to_datetime(labels),
    )


def test_is_period_end_weekly_is_friday():
    assert _is_period_end(date(2024, 1, 5), "1wk") is True    # Friday
    assert _is_period_end(date(2024, 1, 3), "1wk") is False   # Wednesday
    assert _is_period_end(date(2024, 1, 8), "1wk") is False   # Monday


def test_is_period_end_monthly_is_last_business_day():
    assert _is_period_end(date(2024, 1, 31), "1mo") is True   # Wed, month end
    assert _is_period_end(date(2024, 1, 30), "1mo") is False
    # March 2024 ends Sunday 03-31 -> Friday 03-29 is the last business day
    assert _is_period_end(date(2024, 3, 29), "1mo") is True


def test_is_period_end_intraday_and_daily_always_true():
    # the evening screen runs after the close, so 1d/4h last bars are complete
    assert _is_period_end(date(2024, 1, 3), "1d") is True
    assert _is_period_end(date(2024, 1, 3), "4h") is True


def test_latest_completed_bar_skips_partial_weekly_bucket():
    # frame: completed week (Fri 01-05) + in-progress bucket (relabeled Wed 01-10)
    f = _weekly_frame(["2024-01-05", "2024-01-10"])
    got = _latest_completed_bar(f, "1wk", today=date(2024, 1, 10))  # Wednesday
    assert got is not None
    assert got["bar_date"] == date(2024, 1, 5)  # the COMPLETED bar, not the partial


def test_latest_completed_bar_uses_last_bar_on_period_end():
    f = _weekly_frame(["2024-01-05", "2024-01-12"])
    got = _latest_completed_bar(f, "1wk", today=date(2024, 1, 12))  # Friday
    assert got is not None
    assert got["bar_date"] == date(2024, 1, 12)  # bucket completes today


def test_latest_completed_bar_daily_is_last_bar_stamped_today():
    f = _weekly_frame(["2024-01-02", "2024-01-03"])  # same columns work for 1d
    got = _latest_completed_bar(f, "1d", today=date(2024, 1, 3))
    assert got is not None
    assert got["bar_date"] == date(2024, 1, 3)


def test_latest_completed_bar_none_when_only_partial_exists():
    f = _weekly_frame(["2024-01-10"])  # a single in-progress bucket mid-week
    assert _latest_completed_bar(f, "1wk", today=date(2024, 1, 10)) is None

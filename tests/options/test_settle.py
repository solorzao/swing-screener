# ruff: noqa: DTZ001 -- naive US/Eastern lab-clock fixtures matching settle.py's naive-Eastern bar convention
from datetime import datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade
from swing_screener.db.session import get_engine
from swing_screener.options.settle import settle_open_trades
from tests.conftest import make_bars


def _trade(**kw) -> OptionPaperTrade:
    base = {"account": "options-lab", "strategy": "gex", "underlying": "SPY", "direction": "long",
                "opened_at": datetime(2026, 7, 13, 9, 35), "entry": 100.0, "stop": 99.0, "target": 102.0}
    base.update(kw)
    return OptionPaperTrade(**base)


def _settle_one(trade, rows, start: str = "2026-07-13 09:30"):
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(trade)
        s.commit()
        bars = make_bars(rows, start=start, freq="5min")
        result = settle_open_trades(s, bars_by_underlying={"SPY": bars})
        s.commit()
        return s.get(OptionPaperTrade, trade.id), result


_QUIET = {"open": 100, "high": 100.6, "low": 99.6, "close": 100.5}  # touches neither 99 nor 102


def test_target_touch_wins() -> None:
    rows = [{"open": 100, "high": 100.5, "low": 99.8, "close": 100.2},
            {"open": 100.2, "high": 102.5, "low": 100.0, "close": 102.2}]
    t, _ = _settle_one(_trade(), rows)
    assert t.status == "closed" and t.exit_reason == "target"
    assert t.exit_price == 102.0
    assert t.realized_r == 2.0


def test_stop_touch_loses_one_r() -> None:
    rows = [{"open": 100, "high": 100.2, "low": 98.9, "close": 99.0}]
    t, _ = _settle_one(_trade(), rows)
    assert t.exit_reason == "stop" and t.realized_r == -1.0


def test_both_in_one_bar_is_worst_case_stop() -> None:
    rows = [{"open": 100, "high": 102.5, "low": 98.9, "close": 101.0}]
    t, _ = _settle_one(_trade(), rows)
    assert t.exit_reason == "stop" and t.realized_r == -1.0


def test_neither_touched_settles_eod_flat() -> None:
    # complete session: last bar starts 15:55 (spans the 16:00 close)
    rows = [_QUIET] * 3
    t, _ = _settle_one(_trade(), rows, start="2026-07-13 15:45")
    assert t.exit_reason == "eod_flat"
    assert t.exit_price == 100.5
    assert t.realized_r == 0.5  # (100.5 - 100) / (100 - 99)


def test_short_direction_mirrors() -> None:
    rows = [{"open": 100, "high": 100.4, "low": 97.9, "close": 98.0}]
    t, _ = _settle_one(_trade(direction="short", stop=101.0, target=98.0), rows)
    assert t.exit_reason == "target" and t.realized_r == 2.0


def test_bars_before_open_are_ignored() -> None:
    # 09:30 bar spikes through both levels BEFORE the 09:35 open; the remaining
    # 77 quiet bars run the full session to 15:55 so eod_flat applies.
    rows = [{"open": 100, "high": 102.5, "low": 98.5, "close": 100.0}] + [_QUIET] * 77
    t, _ = _settle_one(_trade(opened_at=datetime(2026, 7, 13, 9, 35)), rows)
    assert t.exit_reason == "eod_flat"  # the 09:30 bar's touches don't count
    assert t.exit_price == 100.5


def test_incomplete_session_leaves_trade_open_instead_of_eod_flat() -> None:
    """An intraday settle run (frame's last 5m bar at 13:00) must NOT flatten an
    untouched trade at a mid-session price -- closed status is immutable, so an
    early eod_flat is a permanent mis-grade (2026-07-17 audit, H2)."""
    rows = [_QUIET] * 3  # 12:50, 12:55, 13:00 -- session still in progress
    t, res = _settle_one(_trade(), rows, start="2026-07-13 12:50")
    assert t.status == "open"
    assert t.exit_reason is None
    assert res.settled == 0
    assert res.skipped_incomplete_session == 1


def test_intraday_stop_hit_still_settles() -> None:
    # only the eod_flat fallback is gated on session completeness: a stop that
    # genuinely traded intraday settles even when the session is incomplete
    rows = [{"open": 100, "high": 100.2, "low": 98.9, "close": 99.0}]  # 13:00 bar hits the stop
    t, res = _settle_one(_trade(), rows, start="2026-07-13 13:00")
    assert t.status == "closed" and t.exit_reason == "stop"
    assert res.settled == 1 and res.skipped_incomplete_session == 0


def test_stale_frame_touch_never_settles_todays_trade() -> None:
    """Yesterday's session contains a stop touch, but the trade opened TODAY:
    walking the stale frame (via the all-bars-precede-open fallback) would close
    the trade at yesterday's level, BEFORE it opened -- closed_at < opened_at,
    negative hold_minutes, and closed rows are immutable (review follow-up)."""
    rows = [{"open": 100, "high": 100.2, "low": 98.9, "close": 99.0}] + [_QUIET] * 2
    t, res = _settle_one(_trade(opened_at=datetime(2026, 7, 14, 9, 35)),
                         rows, start="2026-07-13 15:45")
    assert t.status == "open"
    assert res.settled == 0 and res.skipped_incomplete_session == 1


def test_same_day_entry_after_last_bar_still_settles() -> None:
    # the fallback's intended case: the trade opened mid-bar (after the bar's
    # 13:00 stamp) so all bars precede opened_at -- a SAME-DAY touch still
    # settles rather than lingering open forever
    rows = [{"open": 100, "high": 100.2, "low": 98.9, "close": 99.0}]
    t, res = _settle_one(_trade(opened_at=datetime(2026, 7, 13, 13, 2)),
                         rows, start="2026-07-13 13:00")
    assert t.status == "closed" and t.exit_reason == "stop"
    assert res.settled == 1


def test_stale_prior_day_frame_leaves_trade_open() -> None:
    # trade opened on the 14th but the frame is the 13th's (complete) session:
    # the all-bars-precede-open fallback must not eod_flat today's trade at
    # YESTERDAY's close
    t, res = _settle_one(_trade(opened_at=datetime(2026, 7, 14, 9, 35)),
                         [_QUIET] * 3, start="2026-07-13 15:45")
    assert t.status == "open"
    assert res.skipped_incomplete_session == 1

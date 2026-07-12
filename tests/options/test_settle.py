from datetime import datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade
from swing_screener.db.session import get_engine
from swing_screener.options.settle import settle_open_trades
from tests.conftest import make_bars


def _trade(**kw) -> OptionPaperTrade:
    base = dict(account="options-lab", strategy="gex", underlying="SPY", direction="long",
                opened_at=datetime(2026, 7, 13, 9, 35), entry=100.0, stop=99.0, target=102.0)
    base.update(kw)
    return OptionPaperTrade(**base)


def _settle_one(trade, rows):
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(trade)
        s.commit()
        bars = make_bars(rows, start="2026-07-13 09:30", freq="5min")
        result = settle_open_trades(s, bars_by_underlying={"SPY": bars})
        s.commit()
        return s.get(OptionPaperTrade, trade.id), result


def test_target_touch_wins() -> None:
    rows = [dict(open=100, high=100.5, low=99.8, close=100.2),
            dict(open=100.2, high=102.5, low=100.0, close=102.2)]
    t, _ = _settle_one(_trade(), rows)
    assert t.status == "closed" and t.exit_reason == "target"
    assert t.exit_price == 102.0
    assert t.realized_r == 2.0


def test_stop_touch_loses_one_r() -> None:
    rows = [dict(open=100, high=100.2, low=98.9, close=99.0)]
    t, _ = _settle_one(_trade(), rows)
    assert t.exit_reason == "stop" and t.realized_r == -1.0


def test_both_in_one_bar_is_worst_case_stop() -> None:
    rows = [dict(open=100, high=102.5, low=98.9, close=101.0)]
    t, _ = _settle_one(_trade(), rows)
    assert t.exit_reason == "stop" and t.realized_r == -1.0


def test_neither_touched_settles_eod_flat() -> None:
    rows = [dict(open=100, high=100.6, low=99.6, close=100.5)] * 3
    t, _ = _settle_one(_trade(), rows)
    assert t.exit_reason == "eod_flat"
    assert t.exit_price == 100.5
    assert t.realized_r == 0.5  # (100.5 - 100) / (100 - 99)


def test_short_direction_mirrors() -> None:
    rows = [dict(open=100, high=100.4, low=97.9, close=98.0)]
    t, _ = _settle_one(_trade(direction="short", stop=101.0, target=98.0), rows)
    assert t.exit_reason == "target" and t.realized_r == 2.0


def test_bars_before_open_are_ignored() -> None:
    rows = [dict(open=100, high=102.5, low=98.5, close=100.0),  # 09:30 bar: pre-open spike
            dict(open=100, high=100.4, low=99.6, close=100.2)]
    t, _ = _settle_one(_trade(opened_at=datetime(2026, 7, 13, 9, 35)), rows)
    assert t.exit_reason == "eod_flat"  # the 09:30 bar's touches don't count

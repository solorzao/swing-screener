from datetime import date

import pytest
from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import Trade
from swing_screener.db.session import get_engine


def _trade(ticker="AAPL"):
    return Trade(ticker=ticker, timeframe="1d", horizon="medium", entry_date=date(2024, 1, 2),
                 entry_price=100.0, size=10.0, stop=95.0, target=110.0)


def test_add_and_list_open_trades():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        t = repo.add_trade(s, _trade())
        assert t.id is not None and t.status == "open"
        assert [x.ticker for x in repo.get_open_trades(s)] == ["AAPL"]
        assert repo.get_closed_trades(s) == []


def test_close_trade_moves_to_closed():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        t = repo.add_trade(s, _trade())
        closed = repo.close_trade(s, t.id, exit_date=date(2024, 1, 9),
                                  exit_price=108.0, exit_reason="target")
        assert closed.status == "closed" and closed.exit_price == 108.0
        assert repo.get_open_trades(s) == []
        assert [x.ticker for x in repo.get_closed_trades(s)] == ["AAPL"]


def test_close_trade_already_closed_raises_distinct_error():
    # Re-closing would silently overwrite the recorded exit; the guard raises
    # AlreadyClosedError -- a ValueError SUBCLASS, so callers catching ValueError
    # keep working, while the cockpit endpoint maps it to 409 (unknown id -> 404).
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        t = repo.add_trade(s, _trade())
        repo.close_trade(s, t.id, exit_date=date(2024, 1, 9),
                         exit_price=108.0, exit_reason="target")
        with pytest.raises(repo.AlreadyClosedError):
            repo.close_trade(s, t.id, exit_date=date(2024, 1, 10),
                             exit_price=109.0, exit_reason="manual")
        closed = s.get(Trade, t.id)
        assert closed is not None and closed.exit_price == 108.0  # first exit survives
        assert issubclass(repo.AlreadyClosedError, ValueError)
        # the unknown-id flavor stays a PLAIN ValueError, never the subclass
        with pytest.raises(ValueError) as err:
            repo.close_trade(s, 999, exit_date=date(2024, 1, 9),
                             exit_price=1.0, exit_reason="manual")
        assert not isinstance(err.value, repo.AlreadyClosedError)


def test_close_trade_with_event_carries_both_rows():
    # the cockpit's manual close: the trade UPDATE and its ExitEvent ride ONE
    # commit (all-or-nothing -- see the function docstring), and the guard raises
    # exactly like close_trade.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        t = repo.add_trade(s, _trade())
        closed, event = repo.close_trade_with_event(
            s, t.id, exit_date=date(2024, 1, 9), exit_price=108.0,
            exit_reason="target", event_reason="manual_close",
            event_message="AAPL closed manually @ 108", created_date=date(2024, 1, 9))
        assert closed.status == "closed" and closed.exit_price == 108.0
        assert event.id is not None and event.trade_id == t.id
        assert event.reason == "manual_close" and event.is_paper is False
        assert event.tier == "" and event.account == "research"
        with pytest.raises(repo.AlreadyClosedError):
            repo.close_trade_with_event(
                s, t.id, exit_date=date(2024, 1, 10), exit_price=1.0,
                exit_reason="x", event_reason="manual_close", event_message="",
                created_date=date(2024, 1, 10))

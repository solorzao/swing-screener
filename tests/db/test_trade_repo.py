from datetime import date

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


def test_update_trade_patches_fields():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        t = repo.add_trade(s, _trade())
        updated = repo.update_trade(s, t.id, stop=97.0, notes="raised stop")
        assert updated.stop == 97.0 and updated.notes == "raised stop"

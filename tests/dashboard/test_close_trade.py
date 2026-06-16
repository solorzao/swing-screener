from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from swing_screener.dashboard import quotes
from swing_screener.db import repo
from swing_screener.db.models import Trade
from swing_screener.db.session import get_engine

APP = str(Path(__file__).parents[2] / "src" / "swing_screener" / "dashboard" / "app.py")


def test_close_trade_from_active_view(tmp_path, monkeypatch):
    # Seed one open AMD trade with a live quote, drive the inline close form on the
    # Active Trades view, and assert the trade transitions open -> closed.
    url = f"sqlite:///{tmp_path / 'close.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    monkeypatch.setattr(quotes, "latest_closes", lambda tickers, **kw: {"AMD": 104.0})
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium", entry_date=date.today(),
                    entry_price=100.0, size=10.0, stop=95.0, target=110.0))
        s.commit()

    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Active Trades").run()
    assert not at.exception

    # Drive the close form: selectbox -> the AMD option, exit price, reason, submit.
    at.selectbox[0].select(at.selectbox[0].options[0]).run()
    at.number_input[0].set_value(108.0)
    at.text_input[0].set_value("target")
    at.button[0].click().run()

    assert not at.exception

    with Session(engine) as s:
        assert repo.get_open_trades(s) == []
        closed = repo.get_closed_trades(s)
        assert len(closed) == 1
        assert closed[0].ticker == "AMD"
        assert closed[0].exit_price == 108.0
        assert closed[0].exit_reason == "target"

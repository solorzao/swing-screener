from pathlib import Path

from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from swing_screener.db import repo
from swing_screener.db.session import get_engine

APP = str(Path(__file__).parents[2] / "src" / "swing_screener" / "dashboard" / "app.py")


def test_trade_entry_adds_valid_trade(tmp_path, monkeypatch):
    # Drive the Trade Entry form on an empty DB and assert the trade persists.
    url = f"sqlite:///{tmp_path / 'entry.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)

    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Trade Entry").run()
    assert not at.exception

    # Ticker is the only text_input on this page (timeframe/horizon keep defaults).
    at.text_input[0].set_value("AMD")
    # number_inputs in document order: entry price, size, stop, target.
    at.number_input[0].set_value(100.0)
    at.number_input[1].set_value(10.0)
    at.number_input[2].set_value(95.0)
    at.number_input[3].set_value(110.0)
    at.button[0].click().run()

    assert not at.exception

    engine = get_engine(url)
    with Session(engine) as s:
        open_trades = repo.get_open_trades(s)
        assert len(open_trades) == 1
        assert open_trades[0].ticker == "AMD"


def test_trade_entry_rejects_bad_stop(tmp_path, monkeypatch):
    # An invalid stop (>= entry) warns and does NOT persist.
    url = f"sqlite:///{tmp_path / 'entry_bad.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)

    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Trade Entry").run()

    at.text_input[0].set_value("AMD")
    at.number_input[0].set_value(100.0)  # entry
    at.number_input[1].set_value(10.0)  # size
    at.number_input[2].set_value(105.0)  # stop >= entry -> invalid
    at.number_input[3].set_value(110.0)  # target
    at.button[0].click().run()

    assert not at.exception
    assert len(at.warning) == 1

    engine = get_engine(url)
    with Session(engine) as s:
        assert repo.get_open_trades(s) == []

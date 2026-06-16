"""AppTest coverage for the Universe and Digest Log reference views."""

from pathlib import Path

from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from swing_screener.db.models import Universe
from swing_screener.db.session import get_engine

APP = str(Path(__file__).parents[2] / "src" / "swing_screener" / "dashboard" / "app.py")


def _seed_universe(url):
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([
            Universe(ticker="AMD", name="Advanced Micro", exchange="NASDAQ",
                     market_cap=2.5e11, avg_dollar_volume=3.0e9),
            Universe(ticker="NVDA", name="Nvidia", exchange="NASDAQ",
                     market_cap=3.0e12, avg_dollar_volume=4.0e10),
        ])
        s.commit()


def test_universe_view_lists_tickers(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'universe.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    _seed_universe(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Universe").run()
    assert not at.exception

    table = at.dataframe[0].value
    assert set(table["ticker"]) == {"AMD", "NVDA"}


def test_universe_search_filters(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'universe.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    _seed_universe(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Universe").run()

    # The search box is the only text input on the Universe page.
    at.text_input[0].set_value("nv").run()
    assert not at.exception
    table = at.dataframe[0].value
    assert set(table["ticker"]) == {"NVDA"}


def test_universe_empty_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'universe.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Universe").run()
    assert not at.exception
    assert len(at.dataframe) == 0
    infos = " ".join(str(getattr(el, "value", "")) for el in at.info)
    assert "Universe is empty" in infos

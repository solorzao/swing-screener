from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from swing_screener.dashboard import quotes
from swing_screener.db.models import PaperTrade, Signal, Trade
from swing_screener.db.session import get_engine

APP = str(Path(__file__).parents[2] / "src" / "swing_screener" / "dashboard" / "app.py")


def _seed(url):
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(Signal(run_date=date.today(), ticker="AMD", timeframe="1d", horizon="medium",
                     score=0.9, rank=1, trigger_close=100.0, atr=4.0, rsi=55.0,
                     entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0))
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium", entry_date=date.today(),
                    entry_price=100.0, size=10.0, stop=95.0, target=110.0))
        s.add(PaperTrade(ticker="AMD", timeframe="1d", horizon="medium", signal_score=0.9,
                         rank=1, fill_status="filled", stop=95.0, target=110.0, risk=5.0,
                         status="closed", realized_r=2.0, hold_bars=4, exit_date=date.today()))
        s.commit()


def test_app_renders_on_empty_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SWING_DB_URL", f"sqlite:///{tmp_path / 'dash.sqlite'}")
    at = AppTest.from_file(APP).run()
    assert not at.exception
    assert any("Swing Screener" in t.value for t in at.title)
    # six tabs render
    assert len(at.tabs) == 6


def test_app_renders_with_seeded_data(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'dash.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    monkeypatch.setattr(quotes, "latest_closes", lambda tickers, **kw: {"AMD": 104.0})
    _seed(url)
    at = AppTest.from_file(APP).run()
    assert not at.exception
    assert len(at.tabs) == 6
    # the seeded ticker surfaces somewhere across the rendered tabs
    rendered = " ".join(str(getattr(el, "value", "")) for el in at.markdown)
    assert "AMD" in rendered or any("AMD" in str(df.value.to_string()) for df in at.dataframe)

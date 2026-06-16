from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from swing_screener.db.models import Trade
from swing_screener.db.session import get_engine

APP = str(Path(__file__).parents[2] / "src" / "swing_screener" / "dashboard" / "app.py")


def _seed_closed(url):
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium",
                    entry_date=date(2026, 1, 1), entry_price=100.0, size=10.0,
                    stop=95.0, target=110.0, status="closed",
                    exit_date=date(2026, 1, 5), exit_price=110.0, exit_reason="target"))
        s.add(Trade(ticker="NVDA", timeframe="1d", horizon="medium",
                    entry_date=date(2026, 1, 2), entry_price=200.0, size=5.0,
                    stop=190.0, target=220.0, status="closed",
                    exit_date=date(2026, 1, 6), exit_price=190.0, exit_reason="stop"))
        s.commit()


def test_closed_trades_renders_metric_and_equity_curve(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'closed.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    _seed_closed(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Closed Trades").run()
    assert not at.exception

    # Cumulative realized P/L metric: +100 (AMD) + -50 (NVDA) = +50.00.
    metrics = [m for m in at.metric if m.label == "Cumulative realized P/L"]
    assert len(metrics) == 1
    assert metrics[0].value == "$50.00"

    # Equity curve is an Altair chart element.
    assert len(at.get("vega_lite_chart")) >= 1


def test_closed_trades_empty_state(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'empty.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Closed Trades").run()
    assert not at.exception
    assert not at.get("vega_lite_chart")

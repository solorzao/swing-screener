"""/api/journal/records dispatches personal books to their own producers."""

from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import create_app
from swing_screener.db.models import OptionPaperTrade, Trade
from swing_screener.db.session import get_engine


def _app(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'j.db').as_posix()}"
    engine = get_engine(url)
    return TestClient(create_app(url, edge_dir=tmp_path)), engine


def test_manual_equity_records_come_from_the_trade_table(tmp_path: Path):
    client, engine = _app(tmp_path)
    with Session(engine) as s:
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium", entry_date=date(2026, 7, 1),
                    entry_price=100.0, size=1.0, stop=95.0, target=110.0, status="closed",
                    exit_price=110.0, exit_date=date(2026, 7, 5), exit_reason="target"))
        s.commit()
    rows = client.get("/api/journal/records?book=manual_equity").json()
    assert [r["symbol"] for r in rows] == ["AMD"]
    assert rows[0]["unit"] == "R" and rows[0]["r"] == 2.0


def test_robinhood_records_are_dollar_unit(tmp_path: Path):
    client, engine = _app(tmp_path)
    with Session(engine) as s:
        s.add(OptionPaperTrade(account="robinhood", strategy="gex", underlying="SPY",
                               direction="long", opened_at=datetime(2026, 7, 1, 10, 0),
                               closed_at=datetime(2026, 7, 1, 15, 0), premium_pnl=42.0,
                               status="closed"))
        s.commit()
    rows = client.get("/api/journal/records?book=robinhood").json()
    assert rows[0]["symbol"] == "SPY" and rows[0]["unit"] == "$" and rows[0]["r"] == 42.0

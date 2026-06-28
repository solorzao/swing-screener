import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db.models import MarketReport
from swing_screener.db.session import get_engine
from swing_screener.notify.market_analysis import deterministic_market_analysis
from swing_screener.notify.market_body import compose_market_body
from swing_screener.notify.market_run import run_market_report
from swing_screener.pipeline.market import gather_market_facts

CFG = StrategyConfig()


def _rising(n=600, start=300.0, slope=0.3):
    c = np.array([start + slope * i for i in range(n)], dtype=float)
    o = np.r_[c[0], c[:-1]]
    idx = pd.date_range("2023-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.5, "low": np.minimum(o, c) - 0.5,
                         "close": c, "volume": np.full(n, 1e6)}, index=idx)


def _fake_fetch(mapping):
    return lambda ticker: mapping.get(ticker)


def test_compose_market_body_subject_and_text():
    facts = gather_market_facts(spy_daily=_rising(), cfg=CFG)
    body = compose_market_body(facts, deterministic_market_analysis(facts))
    assert body.subject.startswith("Market Weather")
    assert "BULL aligned" in body.subject
    assert "alignment: aligned_bull" in body.text     # the facts block is appended
    assert "<h2>" in body.html


def test_run_market_report_persists_and_emails(tmp_path):
    sent: list[dict] = []
    db = f"sqlite:///{tmp_path / 'm.db'}"
    facts = run_market_report(db_url=db, to="me@example.com", fetch=_fake_fetch({"SPY": _rising()}),
                              smtp_send=lambda **kw: sent.append(kw), cfg=CFG)
    assert facts is not None
    assert sent and sent[0]["subject"].startswith("Market Weather")
    assert sent[0]["to"] == "me@example.com"

    eng = get_engine(db)
    try:
        with Session(eng) as s:
            rows = list(s.scalars(select(MarketReport)))
        assert len(rows) == 1
        assert rows[0].ha_alignment == "aligned_bull" and rows[0].is_deep is False
    finally:
        eng.dispose()


def test_run_market_report_skips_without_spy(tmp_path):
    sent: list[dict] = []
    out = run_market_report(db_url=f"sqlite:///{tmp_path / 'm.db'}", to="me@example.com",
                            fetch=_fake_fetch({}), smtp_send=lambda **kw: sent.append(kw), cfg=CFG)
    assert out is None and not sent

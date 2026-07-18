from dataclasses import replace

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


class _Usage:
    input_tokens = 1000
    output_tokens = 500


class _FakeClient:
    """Minimal Anthropic stand-in returning a fixed labelled reply (no network)."""
    def __init__(self, text, usage=None):
        self._text = text
        self._usage = usage

    @property
    def messages(self):
        text = self._text

        class _Block:
            type = "text"
            def __init__(self):
                self.text = text
                self.citations = []

        class _Resp:
            content = [_Block()]

        _Resp.usage = self._usage

        class _M:
            def create(self, **kw):
                return _Resp()
        return _M()


def test_compose_market_body_subject_and_text():
    facts = gather_market_facts(spy_daily=_rising(), cfg=CFG)
    body = compose_market_body(facts, deterministic_market_analysis(facts))
    assert body.subject.startswith("Market Weather")
    assert "BULL aligned" in body.subject
    assert "alignment: aligned_bull" in body.text     # the facts block is appended
    assert "<h2>" in body.html


def test_run_market_report_uses_llm_when_enabled(tmp_path, monkeypatch):
    # default cfg now has market_report_enabled=True; an injected fake client keeps it offline.
    monkeypatch.delenv("SWING_MARKET_REPORT", raising=False)  # absent env = LLM on (today's behavior)
    sent: list[dict] = []
    db = f"sqlite:///{tmp_path / 'm.db'}"
    client = _FakeClient("CORE: Risk-on tape.\nRegime: SPY aligned bull.\nRisk: complacency.")
    facts = run_market_report(db_url=db, to="me@example.com", fetch=_fake_fetch({"SPY": _rising()}),
                              smtp_send=lambda **kw: sent.append(kw), client=client,
                              migrate_fn=lambda _u: None, cfg=CFG)
    assert facts is not None
    assert sent and sent[0]["subject"].startswith("Market Weather")
    assert sent[0]["to"] == "me@example.com"
    eng = get_engine(db)
    try:
        with Session(eng) as s:
            rows = list(s.scalars(select(MarketReport)))
        assert len(rows) == 1
        assert rows[0].ha_alignment == "aligned_bull" and rows[0].is_deep is True
        assert "Risk-on tape" in rows[0].core
    finally:
        eng.dispose()


def test_run_market_report_deterministic_when_disabled(tmp_path):
    db = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = replace(CFG, market_report_enabled=False)
    facts = run_market_report(db_url=db, to="me@example.com", fetch=_fake_fetch({"SPY": _rising()}),
                              smtp_send=lambda **kw: None, migrate_fn=lambda _u: None, cfg=cfg)
    assert facts is not None
    eng = get_engine(db)
    try:
        with Session(eng) as s:
            rows = list(s.scalars(select(MarketReport)))
        assert len(rows) == 1 and rows[0].is_deep is False
    finally:
        eng.dispose()


def test_env_off_switch_forces_deterministic_no_llm(tmp_path, monkeypatch):
    """E6: SWING_MARKET_REPORT=0 must skip the LLM even though StrategyConfig's
    code-level switch stays True -- same semantics as market_report_enabled=False:
    the deterministic row still persists and the email still sends, just at $0."""
    monkeypatch.setenv("SWING_MARKET_REPORT", "0")
    sent: list[dict] = []
    db = f"sqlite:///{tmp_path / 'm.db'}"
    client = _FakeClient("CORE: SHOULD NOT APPEAR.", usage=_Usage())
    facts = run_market_report(db_url=db, to="me@example.com",
                              fetch=_fake_fetch({"SPY": _rising()}),
                              smtp_send=lambda **kw: sent.append(kw), client=client,
                              migrate_fn=lambda _u: None, cfg=CFG)
    assert facts is not None and len(sent) == 1
    eng = get_engine(db)
    try:
        with Session(eng) as s:
            row = s.scalars(select(MarketReport)).one()
        assert row.is_deep is False                      # the LLM path never ran
        assert "SHOULD NOT APPEAR" not in row.core
        assert row.est_cost_usd is None                  # no billed call -> honest NULL
    finally:
        eng.dispose()


def test_est_cost_usd_is_persisted_on_the_llm_path(tmp_path, monkeypatch):
    """E6 spend visibility: the one weekly deep call's captured usage lands on the
    MarketReport row as an APPROXIMATE list-price estimate (was captured-then-dropped)."""
    monkeypatch.delenv("SWING_MARKET_REPORT", raising=False)
    db = f"sqlite:///{tmp_path / 'm.db'}"
    client = _FakeClient("CORE: Risk-on tape.\nRisk: complacency.", usage=_Usage())
    facts = run_market_report(db_url=db, to="me@example.com",
                              fetch=_fake_fetch({"SPY": _rising()}),
                              smtp_send=lambda **kw: None, client=client,
                              migrate_fn=lambda _u: None, cfg=CFG)
    assert facts is not None
    eng = get_engine(db)
    try:
        with Session(eng) as s:
            row = s.scalars(select(MarketReport)).one()
        assert row.is_deep is True
        assert row.est_cost_usd is not None and row.est_cost_usd > 0
    finally:
        eng.dispose()


def test_billed_but_empty_llm_reply_still_persists_est_cost(tmp_path, monkeypatch):
    """E3b symmetry: an empty LLM reply was still BILLED -- the deterministic fallback
    row (is_deep False) must carry the captured cost, never a dishonest NULL."""
    monkeypatch.delenv("SWING_MARKET_REPORT", raising=False)
    db = f"sqlite:///{tmp_path / 'm.db'}"
    client = _FakeClient("   ", usage=_Usage())
    facts = run_market_report(db_url=db, to="me@example.com",
                              fetch=_fake_fetch({"SPY": _rising()}),
                              smtp_send=lambda **kw: None, client=client,
                              migrate_fn=lambda _u: None, cfg=CFG)
    assert facts is not None
    eng = get_engine(db)
    try:
        with Session(eng) as s:
            row = s.scalars(select(MarketReport)).one()
        assert row.is_deep is False                      # the reply was unusable
        assert row.est_cost_usd is not None and row.est_cost_usd > 0   # but billed
    finally:
        eng.dispose()


def test_run_market_report_is_idempotent_per_run_date(tmp_path):
    """The Sunday UTC cron pair can double-fire (DST) and a crashed run retries: a second
    run for the same as-of date must not re-analyze, re-email, or insert a second row
    (observed in prod: two identical MarketReport rows + two emails for 2026-06-26)."""
    sent: list[dict] = []
    db = f"sqlite:///{tmp_path / 'm.db'}"
    kwargs = dict(db_url=db, to="me@example.com", fetch=_fake_fetch({"SPY": _rising()}),
                  smtp_send=lambda **kw: sent.append(kw), migrate_fn=lambda _u: None,
                  cfg=replace(CFG, market_report_enabled=False))
    first = run_market_report(**kwargs)
    assert first is not None and len(sent) == 1
    second = run_market_report(**kwargs)  # same data -> same as_of -> a no-op
    assert second is None
    assert len(sent) == 1  # no duplicate email
    eng = get_engine(db)
    try:
        with Session(eng) as s:
            assert len(list(s.scalars(select(MarketReport)))) == 1  # no duplicate row
    finally:
        eng.dispose()


def test_report_persists_before_the_email_is_sent(tmp_path):
    """PERSIST-then-SEND: a send crash must leave the report row behind, so the retry run
    skips cleanly (no second Opus bill, no duplicate email) instead of redoing everything.
    The old order (send first) is what double-mailed on retry."""
    import pytest

    db = f"sqlite:///{tmp_path / 'm.db'}"

    def boom(**kw):
        raise RuntimeError("smtp down")

    with pytest.raises(RuntimeError):
        run_market_report(db_url=db, to="me@example.com", fetch=_fake_fetch({"SPY": _rising()}),
                          smtp_send=boom, migrate_fn=lambda _u: None,
                          cfg=replace(CFG, market_report_enabled=False))
    eng = get_engine(db)
    try:
        with Session(eng) as s:
            assert len(list(s.scalars(select(MarketReport)))) == 1  # persisted despite the crash
    finally:
        eng.dispose()


def test_run_market_report_skips_without_spy(tmp_path):
    sent: list[dict] = []
    out = run_market_report(db_url=f"sqlite:///{tmp_path / 'm.db'}", to="me@example.com",
                            fetch=_fake_fetch({}), smtp_send=lambda **kw: sent.append(kw), cfg=CFG)
    assert out is None and not sent


def test_db_float_maps_nan_to_none_at_the_db_boundary():
    """Defense in depth for the 2026-07-05 incident: even if a future fact computation
    lets a NaN through, the persist boundary must send NULL, never NaN (SQL Server
    rejects NaN floats at the wire -- TDS 8023)."""
    from swing_screener.notify.market_run import _db_float

    assert _db_float(float("nan")) is None
    assert _db_float(None) is None
    assert _db_float(16.15) == 16.15
    assert _db_float(0.0) == 0.0          # falsy but valid -- must survive

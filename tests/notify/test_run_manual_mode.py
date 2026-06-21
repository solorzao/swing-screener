"""send_digest MANUAL wiring (Phase-5 Task 1): ``execution_mode="manual"`` resolves the
existing Phase-3 ``ManualAdapter`` from the real mode (NO adapter injected), batch-dispatches
the run's intents through it, and renders the order ticket -- WITHOUT touching a broker or
opening a position.

The load-bearing properties pinned here, none stubbed:

* ``execution_mode="manual"`` + a deep playbook pick -> the ManualAdapter RECORDS the order
  ticket (a ``"recorded"`` ExecutionLog row under account ``"manual"``); NO PaperTrade is
  opened (money never moves) and NO broker is constructed (``build_broker`` monkeypatched to
  blow up proves it is never reached -- manual needs no venue).
* ``_adapter_for_mode("manual")`` returns a ``ManualAdapter`` (the focused resolver unit).
* ``off`` (the default) is unchanged: no dispatch, no ticket, no ExecutionLog row.

All offline: the conviction analyzer, chart loader, fundamentals/news fetchers, the market
regime are injected -- no LLM/blob/yfinance/broker -- and the manual path needs none anyway.
"""

import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import ExecutionLog, PaperTrade, Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import run
from swing_screener.notify.analysis import ConvictionResult, SignalAnalysis
from swing_screener.notify.market_context import Fundamentals
from swing_screener.pipeline.execution import ManualAdapter
from swing_screener.pipeline.reflect import Verdict

RUN = date(2026, 6, 15)


def _sig(ticker, rank):
    return Signal(run_date=RUN, ticker=ticker, timeframe="1d", horizon="medium",
                  score=0.85, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
                  volatility_tier="med", quality_tier="high",
                  entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
                  chart_path=f"20260615/{ticker}_1d_20260615.png")


def _seed(url, n=1):
    with Session(get_engine(url)) as s:
        s.add_all([_sig(t, i + 1) for i, t in enumerate(["AMD", "AEP", "NVDA"][:n])])
        s.commit()


def _edge_dir(tmp_path):
    edge = tmp_path / "edge"
    edge.mkdir()
    for pt in ("continuation", "reversal"):
        (edge / f"{pt}.md").write_text(f"## Thesis\n\n{pt} playbook.\n", encoding="utf-8")
        verdicts = [Verdict(
            play_type=pt, dimension="score", bucket="0.80-1.00", tier="forward_confirmed",
            n=40, expectancy_r=0.5, ci_low=0.2, n_clusters=8, source="forward")]
        (edge / f"{pt}.verdicts.json").write_text(
            json.dumps([v.__dict__ for v in verdicts]), encoding="utf-8")
    return edge


def _kwargs(tmp_path, url, **extra):
    return dict(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                pdf_dir=tmp_path / "digests", smtp_send=extra.pop("smtp_send", lambda **k: None),
                **extra)


def _insight_kwargs(tmp_path, url, **extra):
    return _kwargs(
        tmp_path, url,
        analyze_conviction_fn=lambda f, **k: ConvictionResult(
            conviction="high", nudge_reason="x", insight="i", is_deep=True),
        deep_analyze_fn=lambda f, **k: SignalAnalysis(core_reason="x", rationale="y"),
        chart_bytes_loader=lambda p: None, edge_dir=_edge_dir(tmp_path),
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False), news_fn=lambda t: [],
        market_trend_fn=lambda: "bull", **extra)


def _enable_deep(monkeypatch):
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)


def _no_broker(monkeypatch):
    """Make ANY broker construction a hard failure: the manual path must never build one."""
    def _boom(_cfg):
        raise AssertionError("build_broker must not be called for execution_mode=manual")
    monkeypatch.setattr(run, "build_broker", _boom)


# ---------------------------------------------------------------------------
# the focused resolver unit: "manual" -> a ManualAdapter (it needs no broker).
# ---------------------------------------------------------------------------
def test_adapter_for_mode_manual_returns_manual_adapter():
    adapter = run._adapter_for_mode("manual")
    assert isinstance(adapter, ManualAdapter)
    assert adapter.name == "manual"


# ---------------------------------------------------------------------------
# manual -> records a ticket, opens NO position, builds NO broker.
# ---------------------------------------------------------------------------
def test_manual_records_a_ticket_opens_no_position_and_builds_no_broker(tmp_path, monkeypatch):
    _enable_deep(monkeypatch)
    monkeypatch.setenv("SWING_EXECUTION_MODE", "manual")
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "300")  # -> sized shares
    _no_broker(monkeypatch)  # building a broker for manual is a test failure
    url = f"sqlite:///{tmp_path / 'manual.sqlite'}"
    _seed(url, n=1)
    sent = []

    # NO execution_adapter injected -> the REAL mode resolution picks the ManualAdapter.
    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k)))

    assert res.sent is True
    with Session(get_engine(url)) as s:
        logs = list(s.scalars(select(ExecutionLog)))
        # exactly one RECORDED ticket, booked under the manual account/mode.
        assert [log.status for log in logs] == ["recorded"]
        assert logs[0].account == "manual" and logs[0].mode == "manual"
        assert logs[0].ticker == "AMD"
        assert logs[0].broker == ""  # manual touches no venue -> no broker name recorded
        # money never moves: NO paper position is opened.
        assert list(s.scalars(select(PaperTrade))) == []
    # the order ticket renders in the body.
    body = sent[-1]["text"].lower()
    assert "order ticket" in body
    assert "recorded" in body


# ---------------------------------------------------------------------------
# off (default) is unchanged: no dispatch, no ticket, no ExecutionLog row.
# ---------------------------------------------------------------------------
def test_off_mode_unchanged_no_dispatch_no_ticket_no_log(tmp_path, monkeypatch):
    _enable_deep(monkeypatch)
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)  # default off
    _no_broker(monkeypatch)  # off never builds a broker either
    url = f"sqlite:///{tmp_path / 'off.sqlite'}"
    _seed(url, n=1)
    sent = []

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k)))

    assert res.sent is True
    body = sent[-1]["text"].lower()
    assert "order ticket" not in body  # the NoOp rendered nothing
    with Session(get_engine(url)) as s:
        assert list(s.scalars(select(ExecutionLog))) == []  # the NoOp wrote nothing

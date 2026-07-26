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
from html import escape

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import ExecutionLog, PaperTrade, Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import run
from swing_screener.notify.analysis import ConvictionResult, SignalAnalysis
from swing_screener.notify.body import OrderTicketLine
from swing_screener.notify.market_context import Fundamentals
from swing_screener.notify.proposals import (
    ProposedOrder,
    build_proposals,
    write_proposals_artifact,
)
from swing_screener.pipeline.execution import ManualAdapter
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.pipeline.reflect import Verdict

RUN = date(2026, 6, 15)


@pytest.fixture(autouse=True)
def _unpark(unpark_continuation):
    """Every test here drives the MANUAL dispatch path THROUGH continuation picks --
    parked by default since the Q6 NULL (see tests/notify/conftest.py); un-park so
    the module keeps its original intent."""


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


# ---------------------------------------------------------------------------
# Task 2: the consolidated "Proposed orders — place on Robinhood" surface.
# manual + recorded tickets -> the section (text + HTML) + the PDF + a JSON artifact.
# ---------------------------------------------------------------------------
def _two_pick_kwargs(tmp_path, monkeypatch, url):
    """Manual mode, two deep continuation picks (top_n=2), sized to real shares."""
    _enable_deep(monkeypatch)
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "2")  # both seeded picks go deep
    monkeypatch.setenv("SWING_EXECUTION_MODE", "manual")
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "300")  # -> sized shares
    _no_broker(monkeypatch)
    _seed(url, n=2)


def test_manual_renders_proposed_orders_section_and_writes_artifact(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'proposals.sqlite'}"
    _two_pick_kwargs(tmp_path, monkeypatch, url)
    sent = []

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k)))

    assert res.sent is True
    text = sent[-1]["text"]
    html = sent[-1]["html"]

    # the consolidated header is present in both bodies.
    assert "Proposed orders — place on Robinhood" in text
    assert "Proposed orders — place on Robinhood" in html

    # both picks render their exact, human-placeable instruction. The seeded signal has
    # entry_ceiling=101 (the limit), stop=95, target=110, HIGH conviction; shares are sized.
    with Session(get_engine(url)) as s:
        logs = {log.ticker: log for log in s.scalars(select(ExecutionLog))}
    assert set(logs) == {"AMD", "AEP"}
    for ticker in ("AMD", "AEP"):
        line = (
            f"Buy {ticker} — limit <= $101.00, {logs[ticker].shares} shares "
            f"(HIGH conviction); then set stop $95.00, target $110.00"
        )
        assert line in text
        assert escape(line) in html  # the HTML body escapes the "<=" angle bracket

    # the structured artifact lands next to the PDF with the recorded levels.
    artifact = tmp_path / "digests" / "proposed_orders_20260615.json"
    assert artifact.exists()
    proposals = json.loads(artifact.read_text(encoding="utf-8"))
    assert {p["ticker"] for p in proposals} == {"AMD", "AEP"}
    amd = next(p for p in proposals if p["ticker"] == "AMD")
    assert amd == {
        "ticker": "AMD", "side": "long", "limit_price": 101.0,
        "shares": logs["AMD"].shares, "stop": 95.0, "target": 110.0,
        "conviction": "high", "play_type": "continuation",
    }

    # the PDF was built (it carries the section too) and attached.
    assert res.pdf_attached is True
    pdf_path = tmp_path / "digests" / "daily_20260615.pdf"
    assert pdf_path.exists()


def test_off_mode_renders_no_proposed_orders_section_and_no_artifact(tmp_path, monkeypatch):
    _enable_deep(monkeypatch)
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)  # default off
    _no_broker(monkeypatch)
    url = f"sqlite:///{tmp_path / 'off_proposals.sqlite'}"
    _seed(url, n=2)
    sent = []

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k)))

    assert res.sent is True
    assert "Proposed orders" not in sent[-1]["text"]
    assert "Proposed orders" not in sent[-1]["html"]
    # no artifact for a non-manual posture.
    assert not (tmp_path / "digests" / "proposed_orders_20260615.json").exists()


# ---------------------------------------------------------------------------
# focused units: skipped tickets are NOT placeable; the artifact write never blocks.
# ---------------------------------------------------------------------------
def _intent(ticker, play_type="continuation"):
    return OrderIntent(
        ticker=ticker, timeframe="1d", play_type=play_type, entry_floor=96.0,
        entry_ceiling=101.0, stop=95.0, target=110.0, conviction="high", shares=40,
        risk_dollars=240.0, edge_played="score:high", key_risk="", insight="i",
        side="long", limit_price=101.0)


def _ticket(intent, status, detail=""):
    return OrderTicketLine(
        side=intent.side, shares=intent.shares, ticker=intent.ticker,
        limit_price=intent.limit_price, stop=intent.stop, target=intent.target,
        status=status, detail=detail)


def test_build_proposals_drops_skipped_tickets():
    recorded, skipped = _intent("AMD"), _intent("AEP")
    tickets = {
        ("AMD", "continuation"): _ticket(recorded, "recorded"),
        ("AEP", "continuation"): _ticket(skipped, "skipped", "limit too low"),
    }
    out = build_proposals([recorded, skipped], tickets)
    assert [p.ticker for p in out] == ["AMD"]  # only the RECORDED ticket is placeable
    assert out[0].instruction() == (
        "Buy AMD — limit <= $101.00, 40 shares (HIGH conviction); "
        "then set stop $95.00, target $110.00")


def test_write_proposals_artifact_never_blocks(tmp_path, caplog):
    # point the "dir" at an existing FILE so the write raises -> swallowed + None.
    clash = tmp_path / "clash"
    clash.write_text("not a dir", encoding="utf-8")
    out = write_proposals_artifact(
        [_to_proposal(_intent("AMD"))], clash, date(2026, 6, 15))
    assert out is None  # failure is swallowed, the digest is never blocked


def _to_proposal(intent):
    return ProposedOrder(
        ticker=intent.ticker, side=intent.side, limit_price=intent.limit_price,
        shares=intent.shares, stop=intent.stop, target=intent.target,
        conviction=intent.conviction, play_type=intent.play_type)


def test_instruction_respects_the_stored_side():
    """instruction() used to hardcode "Buy" regardless of the stored side; a short
    proposal (OrderIntent's side vocabulary is long/short) must render Sell and
    flip the limit bound (2026-07-17 audit M4c)."""
    short = ProposedOrder(
        ticker="AMD", side="short", limit_price=101.0, shares=40, stop=95.0,
        target=110.0, conviction="high", play_type="continuation")
    line = short.instruction()
    assert line.startswith("Sell AMD")
    assert "limit >= $101.00" in line
    # the shipped long side still renders the exact Buy instruction
    long_side = ProposedOrder(
        ticker="AMD", side="long", limit_price=101.0, shares=40, stop=95.0,
        target=110.0, conviction="high", play_type="continuation")
    assert long_side.instruction().startswith("Buy AMD — limit <= $101.00")

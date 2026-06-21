"""send_digest LIVE wiring (Task 7): resolve the live adapter from a configured broker,
batch-dispatch the run's intents through it, and HALT the remaining submits (+ pull resting
orders) if the kill switch flips mid-loop.

The whole suite drives a ``FakeBroker`` injected through the new ``broker=`` seam -- it is
pure, deterministic and venue-free, so the live path is exercised end-to-end and can NEVER
hit a real API. The load-bearing properties pinned here:

* ``execution_mode="live"`` + a FakeBroker -> a deep playbook pick SUBMITS a live order
  (a ``submitted_live`` ExecutionLog carrying the broker_order_id, the broker received it)
  and NO PaperTrade is materialized yet (the reconciler does that later).
* ``execution_mode="live"`` but NO broker configured -> the live adapter is NOT armed: the
  NoOp resolves with a warning, nothing is submitted (a stray live config can't place an
  order without a broker).
* The per-submit KILL SWITCH: when ``execution_mode`` is flipped away from ``live`` between
  submits, the loop stops submitting the rest AND calls ``broker.cancel_all_orders()`` to
  pull resting orders.

All offline: the conviction analyzer, chart loader, fundamentals/news fetchers, the market
regime, AND the broker are injected -- no LLM/blob/yfinance/broker.
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
from swing_screener.pipeline.broker import FakeBroker
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


def _enable_deep(monkeypatch, top_n="1"):
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", top_n)
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)


def _live(monkeypatch):
    """Arm execution_mode=live (a paper FakeBroker bypasses the real-money locks)."""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "live")


# ---------------------------------------------------------------------------
# live + a broker -> a real order is submitted, NO PaperTrade yet, fake-never-real.
# ---------------------------------------------------------------------------
def test_live_with_broker_submits_a_live_order_and_opens_no_position(tmp_path, monkeypatch):
    _enable_deep(monkeypatch)
    _live(monkeypatch)
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "300")  # -> sized shares
    url = f"sqlite:///{tmp_path / 'live.sqlite'}"
    _seed(url, n=1)
    sent = []
    broker = FakeBroker(real_money=False)

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), broker=broker))

    assert res.sent is True
    # the FakeBroker REALLY received the order (keyed by our idempotency key).
    assert len(broker.list_open_orders()) == 1
    with Session(get_engine(url)) as s:
        logs = list(s.scalars(select(ExecutionLog)))
        assert [log.status for log in logs] == ["submitted_live"]
        assert logs[0].account == "live" and logs[0].mode == "live"
        assert logs[0].broker == "fake"
        assert logs[0].broker_order_id == "fake-0"
        # NO position is materialized at submit -- the reconciler does that later.
        assert list(s.scalars(select(PaperTrade))) == []


# ---------------------------------------------------------------------------
# live but NO broker configured -> NoOp + warn, nothing submitted.
# ---------------------------------------------------------------------------
def test_live_without_a_broker_does_not_submit(tmp_path, monkeypatch, caplog):
    _enable_deep(monkeypatch)
    _live(monkeypatch)
    monkeypatch.delenv("SWING_BROKER", raising=False)  # no broker configured
    url = f"sqlite:///{tmp_path / 'nobroker.sqlite'}"
    _seed(url, n=1)
    sent = []

    import logging
    with caplog.at_level(logging.WARNING):
        # no broker injected AND SWING_BROKER unset -> _build_broker returns None.
        res = run.send_digest(**_insight_kwargs(
            tmp_path, url, smtp_send=lambda **k: sent.append(k)))

    assert res.sent is True
    body = sent[-1]["text"].lower()
    assert "order ticket" not in body  # the NoOp rendered nothing
    with Session(get_engine(url)) as s:
        assert list(s.scalars(select(ExecutionLog))) == []  # nothing submitted
    assert any("no broker" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# the KILL SWITCH: flip execution_mode away from live between submits -> the loop
# halts the remaining submits AND calls cancel_all_orders().
# ---------------------------------------------------------------------------
def test_kill_switch_halts_remaining_submits_and_cancels(tmp_path, monkeypatch):
    _enable_deep(monkeypatch, top_n="5")  # so BOTH seeded picks get a deep intent
    _live(monkeypatch)
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "300")
    url = f"sqlite:///{tmp_path / 'kill.sqlite'}"
    _seed(url, n=2)  # AMD, AEP -> two continuation intents
    sent = []
    broker = FakeBroker(real_money=False)

    # A mode_reader the test flips: live for the FIRST submit, "off" for every read after.
    reads = {"n": 0}

    def flipping_mode_reader() -> str:
        reads["n"] += 1
        return "live" if reads["n"] == 1 else "off"

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), broker=broker,
        mode_reader=flipping_mode_reader))

    assert res.sent is True
    with Session(get_engine(url)) as s:
        submitted = list(s.scalars(
            select(ExecutionLog).where(ExecutionLog.status == "submitted_live")))
    # ONLY the first intent was submitted; the loop halted before the second.
    assert len(submitted) == 1
    # and the kill switch pulled the resting order -> the first order is now canceled.
    assert broker.list_open_orders() == []  # cancel_all_orders() was called


# ---------------------------------------------------------------------------
# off (default) + a broker present -> still NO submit, no live rows (off=today).
# ---------------------------------------------------------------------------
def test_off_mode_never_submits_even_with_a_broker(tmp_path, monkeypatch):
    _enable_deep(monkeypatch)
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)  # default off
    url = f"sqlite:///{tmp_path / 'offbroker.sqlite'}"
    _seed(url, n=1)
    sent = []
    broker = FakeBroker(real_money=False)

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), broker=broker))

    assert res.sent is True
    assert broker.list_open_orders() == []  # the broker was never touched
    with Session(get_engine(url)) as s:
        assert list(s.scalars(select(ExecutionLog))) == []  # the NoOp wrote nothing

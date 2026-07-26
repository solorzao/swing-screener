"""send_digest execution-adapter wiring (Task 6): collect the run's OrderIntents,
batch-dispatch them through the INJECTED adapter in one try/except, render the
resulting order ticket + status.

GATED + GRACEFUL is load-bearing and proven here:
* ``execution_mode="off"`` (the default, no adapter injected) is EXACTLY today's
  behavior -- no submit, no ExecutionLog rows, and the body is byte-for-byte the
  same as a run with execution disabled (assert no "Order ticket" line).
* A FAKE adapter is injected everywhere a submit is exercised. It RECORDS its
  calls and returns a canned ``OrderResult`` -- it NEVER touches a broker or opens a
  position, so the suite can never place an order.
* An adapter whose ``submit`` RAISES never blocks the digest: the email still goes
  out; the failure is swallowed + logged.

All offline: the conviction analyzer, chart loader, fundamentals/news fetchers, the
market regime, AND the execution adapter are injected -- no LLM/blob/yfinance/broker.
"""

import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

import pytest

from swing_screener.db.models import ExecutionLog, Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import run
from swing_screener.notify.analysis import ConvictionResult, SignalAnalysis
from swing_screener.notify.market_context import Fundamentals
from swing_screener.pipeline.execution import OrderResult
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.pipeline.reflect import Verdict

RUN = date(2026, 6, 15)


@pytest.fixture(autouse=True)
def _unpark(unpark_continuation):
    """Every test here drives the execution machinery THROUGH continuation picks --
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
    """A temp edge dir with a continuation playbook + verdicts so the insight engine
    runs (and an OrderIntent is built) for the top continuation pick. Reversal gets one
    too so the daily path is well-formed even though we seed only continuation signals."""
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
    """The deep+playbook seam wiring shared by every execution test: a canned
    conviction (high), a deep fn that must NOT be called, offline chart/fundamentals/
    news/regime, and the temp edge dir with playbooks."""
    return _kwargs(
        tmp_path, url,
        analyze_conviction_fn=lambda f, **k: ConvictionResult(
            conviction="high", nudge_reason="x", insight="i", is_deep=True),
        deep_analyze_fn=lambda f, **k: SignalAnalysis(core_reason="x", rationale="y"),
        chart_bytes_loader=lambda p: None, edge_dir=_edge_dir(tmp_path),
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False), news_fn=lambda t: [],
        market_trend_fn=lambda: "bull", **extra)


class _FakeAdapter:
    """Records every submit and returns a canned OrderResult -- NEVER places an order
    or opens a position. Proves the suite can't execute a real trade."""

    name = "fake"

    def __init__(self, result: OrderResult) -> None:
        self.result = result
        self.calls: list[OrderIntent] = []

    def submit(self, intent, *, session, run_date, limits) -> OrderResult:
        self.calls.append(intent)
        return self.result


class _RaisingAdapter:
    name = "boom"

    def __init__(self) -> None:
        self.calls: list[OrderIntent] = []

    def submit(self, intent, *, session, run_date, limits) -> OrderResult:
        self.calls.append(intent)
        raise RuntimeError("broker exploded")


def _enable_deep(monkeypatch):
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)


def test_injected_adapter_gets_one_submit_per_deep_intent_and_renders_ticket(
        tmp_path, monkeypatch):
    _enable_deep(monkeypatch)
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "300")  # -> sized shares
    url = f"sqlite:///{tmp_path / 'exec.sqlite'}"
    _seed(url, n=1)
    sent = []
    adapter = _FakeAdapter(OrderResult(
        status="recorded", account="manual", detail="order ticket recorded"))

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), execution_adapter=adapter))

    assert res.sent is True
    # Exactly ONE submit -- for the single top continuation pick's built OrderIntent.
    assert [i.ticker for i in adapter.calls] == ["AMD"]
    assert adapter.calls[0].side == "long"
    # The order ticket + status is rendered in the body.
    body = sent[-1]["text"].lower()
    assert "order ticket" in body
    assert "recorded" in body


def test_skipped_result_renders_skip_reason(tmp_path, monkeypatch):
    _enable_deep(monkeypatch)
    url = f"sqlite:///{tmp_path / 'skip.sqlite'}"
    _seed(url, n=1)
    sent = []
    adapter = _FakeAdapter(OrderResult(
        status="skipped", account="manual", detail="per-day notional cap: 5000 > 1000"))

    run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), execution_adapter=adapter))

    body = sent[-1]["text"].lower()
    assert "skipped" in body
    assert "per-day notional cap" in body


def test_adapter_raise_never_blocks_the_digest(tmp_path, monkeypatch):
    _enable_deep(monkeypatch)
    url = f"sqlite:///{tmp_path / 'raise.sqlite'}"
    _seed(url, n=1)
    sent = []
    adapter = _RaisingAdapter()

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), execution_adapter=adapter))

    # The dispatch raised, but the email still went out (failure swallowed + logged).
    assert res.sent is True
    assert adapter.calls  # the adapter was reached
    assert len(sent) == 1  # the digest email was still sent
    body = sent[-1]["text"].lower()
    assert "order ticket" not in body  # nothing rendered for the failed dispatch


def test_parked_continuation_never_builds_or_dispatches_intents(tmp_path, monkeypatch):
    """The parked+armed invariant, pinned rather than held by construction: with
    continuation PARKED (the real default -- the REAL StrategyConfig is restored over
    this module's un-park fixture) and execution armed via an injected adapter, NO
    continuation intent is ever built or dispatched (the parked pickers return [], so
    ``_build_picks`` never sees a continuation signal), while the reversal pick's
    intent flows to the adapter exactly as before."""
    from swing_screener.config import StrategyConfig

    monkeypatch.setattr(run, "StrategyConfig", StrategyConfig)  # parked default is back
    _enable_deep(monkeypatch)  # top-1 deep per play type
    url = f"sqlite:///{tmp_path / 'parkexec.sqlite'}"
    _seed(url, n=2)  # AMD + AEP continuation: must never reach the adapter
    with Session(get_engine(url)) as s:
        s.add(Signal(run_date=RUN, ticker="GME", timeframe="1d", horizon="medium",
                     play_type="reversal", strength="confirmed", score=0.9, rank=1,
                     trigger_close=50.0, atr=2.0, rsi=22.0, volatility_tier="med",
                     quality_tier="high", entry_floor=50.0, entry_ceiling=52.0,
                     stop=47.0, target=58.0))
        s.commit()
    sent = []
    adapter = _FakeAdapter(OrderResult(
        status="recorded", account="manual", detail="order ticket recorded"))

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), execution_adapter=adapter))

    assert res.sent is True and res.n_picks == 0 and res.n_reversals == 1
    # ONLY the reversal intent was built and dispatched; continuation appears nowhere.
    assert [(i.ticker, i.play_type) for i in adapter.calls] == [("GME", "reversal")]
    body = sent[-1]["text"]
    assert "AMD" not in body and "AEP" not in body
    assert "Order ticket" in body  # the reversal ticket still renders


def test_off_mode_does_not_dispatch_or_render_or_log(tmp_path, monkeypatch):
    """Default execution_mode="off": no adapter injected, the NoOp resolves -- and the
    run must be EXACTLY today's behavior: no submit, no ExecutionLog rows, and the body
    carries no 'Order ticket' line."""
    _enable_deep(monkeypatch)
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)  # default off
    url = f"sqlite:///{tmp_path / 'off.sqlite'}"
    _seed(url, n=1)
    sent = []

    # No execution_adapter injected -> resolves to NoOpAdapter from mode "off".
    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k)))

    assert res.sent is True
    body = sent[-1]["text"].lower()
    assert "order ticket" not in body  # NOTHING new rendered
    with Session(get_engine(url)) as s:
        assert list(s.scalars(select(ExecutionLog))) == []  # the NoOp wrote nothing

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

from swing_screener.db import guardrails_repo as gr
from swing_screener.db import repo
from swing_screener.db.models import (
    AgentGuardrailEvent,
    DisarmEvent,
    ExecutionLog,
    PaperTrade,
    Signal,
)
from swing_screener.db.session import get_engine
from swing_screener.notify import run
from swing_screener.notify.analysis import ConvictionResult, SignalAnalysis
from swing_screener.notify.market_context import Fundamentals
from swing_screener.pipeline.broker import BrokerOrderSpec, FakeBroker
from swing_screener.pipeline.execution import UNSIZED_DETAIL, OrderResult
from swing_screener.pipeline.insight import OrderIntent
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
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "300")  # sized -> reaches the adapter
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
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "300")  # sized -> reaches the adapter
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


def test_zero_share_intent_never_reaches_adapter_and_renders_unsized(tmp_path, monkeypatch):
    """An UNSIZED intent (no risk unit configured -> size_order floors to 0 shares) is
    INERT: the dispatch loop must never call submit for it -- a qty<=0 order is a
    guaranteed venue 422 -- but the digest still renders an honest 'skipped' ticket."""
    _enable_deep(monkeypatch)
    # no risk unit, no equity -> resolve_risk_unit 0.0 -> shares floored to 0.
    monkeypatch.delenv("SWING_RISK_PER_TRADE_DOLLARS", raising=False)
    monkeypatch.delenv("SWING_ACCOUNT_EQUITY", raising=False)
    url = f"sqlite:///{tmp_path / 'zero.sqlite'}"
    _seed(url, n=1)
    sent = []
    adapter = _FakeAdapter(OrderResult(
        status="recorded", account="manual", detail="order ticket recorded"))

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), execution_adapter=adapter))

    assert res.sent is True
    assert adapter.calls == []  # submit was NEVER called for the unsized intent
    body = sent[-1]["text"].lower()
    assert "order ticket" in body   # the synthetic ticket still renders honestly
    assert UNSIZED_DETAIL in body   # the shared detail constant, verbatim
    assert "skipped" in body


class _CancelCountingBroker(FakeBroker):
    """A FakeBroker that counts cancels, so 'entries pulled ONCE' is assertable."""

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.cancels = 0

    def cancel_order(self, broker_order_id: str) -> None:  # type: ignore[override]
        self.cancels += 1
        super().cancel_order(broker_order_id)


def _entry_spec(key: str, symbol: str, **overrides: object) -> BrokerOrderSpec:
    base: dict[str, object] = dict(client_order_id=key, symbol=symbol, side="buy",
                                   qty=10, order_type="limit", limit_price=100.0,
                                   time_in_force="day")
    base.update(overrides)
    return BrokerOrderSpec(**base)  # type: ignore[arg-type]


def _live_env(monkeypatch):
    """Arm the LIVE dispatch path: deep on, sized intents, execution mode live.
    The broker is always an injected FakeBroker, so no venue can ever be touched."""
    _enable_deep(monkeypatch)
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "300")
    monkeypatch.setenv("SWING_EXECUTION_MODE", "live")


def test_dispatch_loop_halts_batch_on_trip(tmp_path, monkeypatch):
    """A breached breaker BEFORE dispatch: zero submits (the whole batch halts on
    the first guardrails consult), the trip is persisted + swept, and the resting
    entry order is pulled exactly once."""
    _live_env(monkeypatch)
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "3")   # 3 sized intents
    url = f"sqlite:///{tmp_path / 'trip.sqlite'}"
    _seed(url, n=3)
    with Session(get_engine(url)) as s:
        # a -$60 realized live day against a $50 cap: the daily-loss breaker breached.
        s.add(PaperTrade(
            ticker="LOSE", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
            account="live", fill_status="filled", entry_date=date(2026, 6, 10),
            entry_price=50.0, stop=45.0, target=60.0, risk=5.0, status="closed",
            exit_date=RUN, exit_price=44.0, realized_r=-1.2, qty=10))
        s.commit()
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0)
    broker = _CancelCountingBroker()
    broker.submit_order(_entry_spec("rest-1", "TSLA"))     # a resting entry at the venue
    n_armed = len(broker.submitted_specs)
    sent = []

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), broker=broker,
        mode_reader=lambda: "live"))

    assert res.sent is True                                # the digest still went out
    with Session(get_engine(url)) as s:
        g = gr.load_guardrails(s)
        assert g.state == "tripped"                        # the trip persisted
        assert g.sweep_state == "complete"
        assert s.query(AgentGuardrailEvent).filter_by(kind="trip").count() == 1
        assert list(s.scalars(select(ExecutionLog))) == [] # ZERO submits reached an adapter
    assert broker.list_open_orders() == []                 # the entry order was pulled...
    assert broker.cancels == 1                             # ...exactly once
    assert len(broker.submitted_specs) == n_armed          # no NEW venue orders placed


def test_halted_state_stops_dispatch_and_pulls_entries(tmp_path, monkeypatch):
    """A manual HALT (not a breach): the batch stops, entries are pulled and dead
    stops restored (the kill-switch sweep), and NO trip event is recorded."""
    _live_env(monkeypatch)
    url = f"sqlite:///{tmp_path / 'halt.sqlite'}"
    _seed(url, n=1)
    with Session(get_engine(url)) as s:
        assert gr.halt(s, source="test") is True
        # the recorded live ticket the halt sweep copies NVDA's stop level from.
        repo.add_execution_log(
            s, created_date=RUN, ticker="NVDA", timeframe="1d",
            play_type="continuation", run_date=RUN, account="live", mode="live",
            side="buy", limit_price=100.0, shares=8, stop=95.0, target=110.0,
            risk_dollars=40.0, notional=800.0, status="submitted_live",
            detail="live order", idempotency_key="k-nvda")
    broker = FakeBroker()
    broker.submit_order(_entry_spec("rest-1", "TSLA"))     # a resting entry to pull
    entry = broker.submit_order(_entry_spec("k-nvda", "NVDA", qty=8, stop_loss=95.0,
                                            take_profit=110.0))
    broker.fill(entry.broker_order_id, 100.0)              # a filled bracket position...
    for order in list(broker.list_open_orders()):          # ...whose legs already died
        if order.side == "sell":
            broker.cancel_order(order.broker_order_id)
    sent = []

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), broker=broker,
        mode_reader=lambda: "live"))

    assert res.sent is True
    open_orders = broker.list_open_orders()
    assert [o for o in open_orders if o.side == "buy"] == []          # entries pulled
    stops = [o for o in open_orders if o.side == "sell" and o.order_type == "stop"]
    assert [o.symbol for o in stops] == ["NVDA"]                      # stop restored
    restored = broker.submitted_specs[-1]
    assert restored.stop_price == 95.0                     # COPIED from the ticket
    assert restored.client_order_id == f"disarm-stop-NVDA-halt-{RUN:%Y%m%d}"
    with Session(get_engine(url)) as s:
        g = gr.load_guardrails(s)
        assert g.state == "halted"                         # a HALT is not a breach...
        assert g.sweep_state is None
        assert s.query(AgentGuardrailEvent).filter_by(kind="trip").count() == 0
        # only the seeded ticket exists: no submit reached the adapter.
        assert s.query(ExecutionLog).count() == 1
        # the halt sweep moved venue state -> it is on the Auditor's conduct record.
        ev = s.query(DisarmEvent).one()
        assert ev.reason == "halt"
        assert ev.orders_cancelled == 1                    # the resting TSLA entry


def test_tripped_pending_sweep_resumes_even_when_mode_off(tmp_path, monkeypatch):
    """THE resume hoist (2026-07-18 red-team break 1): a tripped book with an
    unfinished sweep is resumed ONCE PER RUN, outside every dispatch gate -- even
    with execution_mode=off (the operator's natural post-trip reaction), zero
    picks, or no live adapter. The broker is built on demand from the seam."""
    _enable_deep(monkeypatch)
    monkeypatch.setenv("SWING_EXECUTION_MODE", "off")      # NoOp adapter, no dispatch
    url = f"sqlite:///{tmp_path / 'resume.sqlite'}"
    _seed(url, n=1)
    with Session(get_engine(url)) as s:
        # last night's trip, swept by nobody (the evening screen had no broker).
        assert gr.trip(s, breaker="max_daily_loss_usd",
                       reason="max daily loss: $-60.00 <= -$50.00",
                       source="screen") is not None
        assert gr.load_guardrails(s).sweep_state == "pending"
    broker = FakeBroker()
    broker.submit_order(_entry_spec("rest-1", "TSLA"))     # a DAY entry, still fillable
    sent = []

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), broker=broker))

    assert res.sent is True
    assert broker.list_open_orders() == []                 # the entry was pulled
    with Session(get_engine(url)) as s:
        g = gr.load_guardrails(s)
        assert g.state == "tripped"
        assert g.sweep_state == "complete"                 # the hoist finished the sweep
        assert s.query(DisarmEvent).one().reason == "guardrail:max_daily_loss_usd"


def test_halted_with_breach_still_records_the_trip(tmp_path, monkeypatch):
    """A breach DURING a manual HALT must still be recorded as a trip (red-team
    break 5): run_date-scoped breakers (daily loss) evaporate when the date
    advances, so skipping evaluation while halted loses the conduct record
    permanently. The repo election deliberately lets a trip overwrite 'halted'."""
    _live_env(monkeypatch)
    url = f"sqlite:///{tmp_path / 'haltbreach.sqlite'}"
    _seed(url, n=1)
    with Session(get_engine(url)) as s:
        assert gr.halt(s, source="test") is True
        s.add(PaperTrade(
            ticker="LOSE", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
            account="live", fill_status="filled", entry_date=date(2026, 6, 10),
            entry_price=50.0, stop=45.0, target=60.0, risk=5.0, status="closed",
            exit_date=RUN, exit_price=44.0, realized_r=-1.2, qty=10))
        s.commit()
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0)
    broker = _CancelCountingBroker()
    broker.submit_order(_entry_spec("rest-1", "TSLA"))
    sent = []

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), broker=broker,
        mode_reader=lambda: "live"))

    assert res.sent is True
    with Session(get_engine(url)) as s:
        g = gr.load_guardrails(s)
        assert g.state == "tripped"                        # the trip OVERWROTE the halt
        assert g.sweep_state == "complete"
        assert s.query(AgentGuardrailEvent).filter_by(kind="trip").count() == 1
        assert list(s.scalars(select(ExecutionLog))) == [] # no submits
    assert broker.list_open_orders() == []                 # sweep ran...
    assert broker.cancels == 1                             # ...exactly once (no halt-sweep double)


def test_mid_batch_trip_halts_remaining_intents(tmp_path, monkeypatch):
    """The mid-batch trip: with max_trades_per_day=1 and three sized intents, the
    first SUBMITS, the second intent's consult trips the breaker, and the rest of
    the batch never reaches the adapter. The sweep pulls the day's own
    just-submitted resting entry (intended -- once the cap is hit, nothing
    unfilled may keep working)."""
    _live_env(monkeypatch)
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "3")   # 3 sized intents
    url = f"sqlite:///{tmp_path / 'midbatch.sqlite'}"
    _seed(url, n=3)
    with Session(get_engine(url)) as s:
        gr.edit_limits(s, source="test", max_trades_per_day=1)
    broker = _CancelCountingBroker()
    sent = []

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), broker=broker,
        mode_reader=lambda: "live"))

    assert res.sent is True
    with Session(get_engine(url)) as s:
        logs = list(s.scalars(select(ExecutionLog)))
        assert [(log.ticker, log.status) for log in logs] == [("AMD", "submitted_live")]
        g = gr.load_guardrails(s)
        assert g.state == "tripped"
        assert g.trip_reason == "max trades/day: 1 >= 1"
        assert g.sweep_state == "complete"
        assert s.query(AgentGuardrailEvent).filter_by(kind="trip").count() == 1
    assert broker.list_open_orders() == []                 # AMD's own entry was pulled
    assert broker.cancels == 1


def test_outcome_write_failure_never_blocks_the_digest(tmp_path, monkeypatch):
    """Red-team break 3: a sweep-outcome write that dies mid-UPDATE poisons the
    shared session; without the guarded write + except-rollback the next session
    use (the autonomy footer) raises PendingRollbackError and NO email goes out."""
    _live_env(monkeypatch)
    url = f"sqlite:///{tmp_path / 'poison.sqlite'}"
    _seed(url, n=1)
    with Session(get_engine(url)) as s:
        s.add(PaperTrade(
            ticker="LOSE", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
            account="live", fill_status="filled", entry_date=date(2026, 6, 10),
            entry_price=50.0, stop=45.0, target=60.0, risk=5.0, status="closed",
            exit_date=RUN, exit_price=44.0, realized_r=-1.2, qty=10))
        s.commit()
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0)

    def _poisoning_outcome(session, **kw):
        # a real mid-write failure that POISONS the session: a failed flush
        # (NOT NULL violation) leaves it inactive -- PendingRollbackError on
        # every next use (the autonomy footer) until somebody rolls back.
        session.add(DisarmEvent(created_at=None, reason="boom", orders_cancelled=0))
        session.flush()

    monkeypatch.setattr(gr, "record_sweep_outcome", _poisoning_outcome)
    broker = FakeBroker()
    broker.submit_order(_entry_spec("rest-1", "TSLA"))
    sent = []

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), broker=broker,
        mode_reader=lambda: "live"))

    assert res.sent is True                                # the digest still went out
    assert len(sent) == 1
    assert broker.list_open_orders() == []                 # the sweep itself ran
    with Session(get_engine(url)) as s:
        g = gr.load_guardrails(s)
        assert g.state == "tripped"                        # the brake held
        assert g.sweep_state == "pending"                  # outcome unrecorded -> re-runnable


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

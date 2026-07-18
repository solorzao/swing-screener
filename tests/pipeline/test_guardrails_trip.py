"""The trip protocol (pipeline/guardrails.py): breaker evaluation + the ORDERED
trip response -- persist FIRST, then sweep, then record the outcome, then mail.

The load-bearing properties proven here:

* PERSIST-FIRST: the tripped state + trip event commit BEFORE any venue call, so
  a broker that explodes mid-sweep still leaves the brake engaged (state
  'tripped', sweep_state 'partial') -- and the failure detail is the exception
  CLASS name only (broker_error_detail's leak posture), never the message.
* SINGLE OWNER: guardrails_repo.trip()'s rows-affected election picks exactly one
  trip-response owner; a lost election means stand down ENTIRELY -- no venue
  call, no DisarmEvent, no email (the trip event itself is still appended by
  trip(): the breach was real, whoever won).
* RESUMABLE: broker=None (the evening-screen secrets gap) persists the trip with
  sweep_state 'pending'; resume_incomplete_sweep re-runs the sweep on the next
  cycle and flips it to 'complete'; an already-'complete' sweep never re-touches
  the venue. Sweep re-runs are safe because pull_entry_orders cancels only
  what's open and ensure_stop_protection skips already-protected symbols.

All in-memory sqlite + FakeBroker -- no venue, no network, no mail.
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db import guardrails_repo as gr
from swing_screener.db import repo
from swing_screener.db.models import AgentGuardrailEvent, DisarmEvent, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline import guardrails as gp
from swing_screener.pipeline.broker import BrokerOrderSpec, FakeBroker

RUN = date(2026, 6, 19)
BREAKER = "max_daily_loss_usd"
REASON = "max daily loss: $-60.00 <= -$50.00"


def _session() -> Session:
    return Session(get_engine("sqlite:///:memory:"))


def _spec(key: str, symbol: str, **overrides: object) -> BrokerOrderSpec:
    base: dict[str, object] = dict(client_order_id=key, symbol=symbol, side="buy",
                                   qty=10, order_type="limit", limit_price=100.0,
                                   time_in_force="day")
    base.update(overrides)
    return BrokerOrderSpec(**base)  # type: ignore[arg-type]


def _closed_live_trade(
    *, ticker: str, entry_price: float = 50.0, exit_price: float = 44.0,
    qty: int | None = 10, exit_date: date = RUN, realized_r: float = -1.0,
) -> PaperTrade:
    """One CLOSED live trade with a broker-stamped qty (the breakers' $ input)."""
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account="live", fill_status="filled", entry_date=date(2026, 6, 10),
        entry_price=entry_price, stop=45.0, target=60.0, risk=5.0, status="closed",
        exit_date=exit_date, exit_price=exit_price, realized_r=realized_r, qty=qty,
    )


class _ExplodingBroker(FakeBroker):
    """A venue that dies on first contact -- with a message that would LEAK
    (host + credential) if any persisted detail ever carried raw exception text."""

    def list_open_orders(self) -> list:  # type: ignore[override]
        raise RuntimeError("connect https://secret-venue.example/orders?key=abc123")


class _CountingBroker(FakeBroker):
    """Counts every venue touch so a stand-down path can assert ZERO of them."""

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.venue_calls = 0

    def submit_order(self, spec: BrokerOrderSpec):  # type: ignore[override]
        self.venue_calls += 1
        return super().submit_order(spec)

    def list_open_orders(self) -> list:  # type: ignore[override]
        self.venue_calls += 1
        return super().list_open_orders()

    def get_positions(self) -> list:  # type: ignore[override]
        self.venue_calls += 1
        return super().get_positions()

    def cancel_order(self, broker_order_id: str) -> None:  # type: ignore[override]
        self.venue_calls += 1
        super().cancel_order(broker_order_id)


# ---------------------------------------------------------------------------
# persist-first: the brake holds even when the venue dies mid-sweep.
# ---------------------------------------------------------------------------
def test_evaluate_and_trip_persists_before_sweep() -> None:
    with _session() as s:
        # a -$60 realized live day against a $50 cap: the daily-loss breaker breached.
        s.add(_closed_live_trade(ticker="LOSE"))
        s.commit()
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0)
        breach = gp.evaluate_breakers(s, run_date=RUN)
        assert breach == (BREAKER, REASON)  # byte-identical to _guardrail_block's string

        trip_id = gp.respond_to_trip(s, breaker=breach[0], reason=breach[1],
                                     source="digest", broker=_ExplodingBroker())

        assert trip_id is not None
        g = gr.load_guardrails(s)
        assert g.state == "tripped"          # the brake HELD despite the venue failure
        assert g.trip_id == trip_id
        assert g.trip_reason == REASON
        assert g.sweep_state == "partial"    # the sweep failure is recorded, not hidden
        trip_event = s.get(AgentGuardrailEvent, trip_id)
        assert trip_event is not None
        assert trip_event.kind == "trip"
        # leak posture: the sweep outcome carries the exception CLASS name only.
        sweep_event = s.query(AgentGuardrailEvent).filter_by(kind="sweep").one()
        assert "RuntimeError" in sweep_event.reason
        assert "secret-venue.example" not in sweep_event.reason
        assert "abc123" not in sweep_event.reason


def test_evaluate_breakers_ignores_state() -> None:
    """State is the trip's OUTCOME, not an input: an already-halted book with a
    breached breaker still reports the breach (the caller gates on state)."""
    with _session() as s:
        s.add(_closed_live_trade(ticker="LOSE"))
        s.commit()
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0)
        assert gr.halt(s, source="test") is True

        assert gp.evaluate_breakers(s, run_date=RUN) == (BREAKER, REASON)


# ---------------------------------------------------------------------------
# single owner: a lost election stands down ENTIRELY.
# ---------------------------------------------------------------------------
def test_trip_owner_runs_sweep_once() -> None:
    with _session() as s:
        first = gp.respond_to_trip(s, breaker=BREAKER, reason=REASON, source="digest",
                                   broker=FakeBroker())
        assert first is not None
        n_disarms = s.query(DisarmEvent).count()

        broker = _CountingBroker()
        emails: list[tuple[int, str, str]] = []
        second = gp.respond_to_trip(
            s, breaker=BREAKER, reason=REASON, source="screen", broker=broker,
            emailer=lambda eid, b, r: emails.append((eid, b, r)))

        assert second is None                              # election lost -> stand down
        assert broker.venue_calls == 0                     # no venue call
        assert s.query(DisarmEvent).count() == n_disarms   # no second DisarmEvent
        assert emails == []                                # no email
        # the breach itself is still on the record (trip() appends unconditionally).
        assert s.query(AgentGuardrailEvent).filter_by(kind="trip").count() == 2


# ---------------------------------------------------------------------------
# the sweep's audit trail: a DisarmEvent the breach scanner already understands.
# ---------------------------------------------------------------------------
def test_sweep_writes_disarm_event_with_guardrail_reason() -> None:
    with _session() as s:
        broker = FakeBroker()
        broker.submit_order(_spec("k1", "AMD"))
        broker.submit_order(_spec("k2", "TSLA"))

        trip_id = gp.respond_to_trip(s, breaker=BREAKER, reason=REASON,
                                     source="digest", broker=broker)

        assert trip_id is not None
        assert gr.load_guardrails(s).sweep_state == "complete"
        assert broker.list_open_orders() == []             # both entries pulled
        ev = s.query(DisarmEvent).one()
        assert ev.reason == f"guardrail:{BREAKER}"
        assert ev.orders_cancelled == 2


def test_sweep_restores_stops_with_trip_keyed_suffix() -> None:
    """The restore's client_order_id is keyed by the TRIP id, so a cross-process
    re-run of the same trip's sweep collapses to the SAME venue order."""
    with _session() as s:
        repo.add_execution_log(
            s, created_date=RUN, ticker="NVDA", timeframe="1d",
            play_type="continuation", run_date=RUN, account="live", mode="live",
            side="buy", limit_price=100.0, shares=8, stop=95.0, target=110.0,
            risk_dollars=40.0, notional=800.0, status="submitted_live",
            detail="live order", idempotency_key="k-nvda")
        broker = FakeBroker()
        entry = broker.submit_order(_spec("k2", "NVDA", qty=8, stop_loss=95.0,
                                          take_profit=110.0))
        broker.fill(entry.broker_order_id, 100.0)
        for order in list(broker.list_open_orders()):      # kill the bracket legs
            if order.side == "sell":
                broker.cancel_order(order.broker_order_id)

        trip_id = gp.respond_to_trip(s, breaker=BREAKER, reason=REASON,
                                     source="digest", broker=broker)

        assert trip_id is not None
        restored = broker.submitted_specs[-1]
        assert restored.side == "sell"
        assert restored.order_type == "stop"
        assert restored.stop_price == 95.0                 # COPIED from the ticket
        assert restored.client_order_id == f"disarm-stop-NVDA-guardrail-{trip_id}"


# ---------------------------------------------------------------------------
# resumability: pending/partial sweeps are re-run; complete ones are left alone.
# ---------------------------------------------------------------------------
def test_pending_or_partial_sweep_rerun_next_cycle() -> None:
    with _session() as s:
        trip_id = gp.respond_to_trip(s, breaker=BREAKER, reason=REASON,
                                     source="screen", broker=None)
        assert trip_id is not None
        assert gr.load_guardrails(s).sweep_state == "pending"

        broker = _CountingBroker()
        broker.submit_order(_spec("k1", "AMD"))            # the entry the resume pulls
        broker.venue_calls = 0
        assert gp.resume_incomplete_sweep(s, broker=broker, source="digest") is True
        assert gr.load_guardrails(s).sweep_state == "complete"
        assert broker.list_open_orders() == []             # the entry was cancelled

        broker.venue_calls = 0
        assert gp.resume_incomplete_sweep(s, broker=broker, source="digest") is False
        assert broker.venue_calls == 0                     # complete -> venue untouched


def test_broker_none_persists_trip_with_pending_sweep() -> None:
    with _session() as s:
        trip_id = gp.respond_to_trip(s, breaker=BREAKER, reason=REASON,
                                     source="screen", broker=None)

        assert trip_id is not None
        g = gr.load_guardrails(s)
        assert g.state == "tripped"
        assert g.trip_id == trip_id
        assert g.sweep_state == "pending"                  # the next cycle owns the sweep
        assert s.query(DisarmEvent).count() == 0           # no sweep ran -> no record


def test_resume_without_broker_is_a_noop() -> None:
    with _session() as s:
        assert gp.respond_to_trip(s, breaker=BREAKER, reason=REASON,
                                  source="screen", broker=None) is not None
        assert gp.resume_incomplete_sweep(s, broker=None, source="screen") is False
        assert gr.load_guardrails(s).sweep_state == "pending"


# ---------------------------------------------------------------------------
# the mail seam: isolated -- a mail failure never aborts (or un-records) a sweep.
# ---------------------------------------------------------------------------
def test_emailer_failure_never_aborts_the_sweep() -> None:
    with _session() as s:
        broker = FakeBroker()
        broker.submit_order(_spec("k1", "AMD"))

        def _boom_mail(eid: int, breaker: str, reason: str) -> None:
            raise RuntimeError("smtp down")

        trip_id = gp.respond_to_trip(s, breaker=BREAKER, reason=REASON,
                                     source="digest", broker=broker,
                                     emailer=_boom_mail)

        assert trip_id is not None                         # the protocol completed
        g = gr.load_guardrails(s)
        assert g.state == "tripped"
        assert g.sweep_state == "complete"                 # the sweep outcome survived
        assert broker.list_open_orders() == []

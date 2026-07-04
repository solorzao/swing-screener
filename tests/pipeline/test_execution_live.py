"""The live execution adapter (``pipeline.execution.LiveAdapter``).

The 4th execution adapter -- the only one that talks to a REAL broker. Unlike the paper
adapter (which OPENS a simulated ``PaperTrade``), the live adapter submits an order to the
broker and records the ``broker_order_id`` ONLY: at submit time the fill price is unknown,
so NO position is materialized here -- the reconciler (Task 5) later turns a filled broker
order into a position. The load-bearing safety: a real-money broker arms ONLY behind all
three locks (``execution_mode == "live"`` AND ``allow_real_money`` AND a ready autonomy gate)
AND with every hard cap set; a PAPER broker (Alpaca paper) needs none of that and bypasses
the whole guard.

The broker is the injectable ``FakeBroker`` (pure, deterministic, venue-free) and the
real-money decision seams (settings + gate readiness) are injected too, so these tests need
no real gate, no DB-backed verdicts, and no network.

In-memory SQLite, no network.
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db.models import ExecutionLog, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.broker import BrokerOrder, BrokerOrderSpec, FakeBroker
from swing_screener.pipeline.execution import LiveAdapter
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.settings import Limits, Settings

RUN = date(2026, 6, 19)
NO_LIMITS = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=None)
# every cap set -- the floor a real-money endpoint must clear (real_money_limits_ok).
FULL_CAPS = Limits(max_daily_notional=100_000.0, max_daily_loss=2.0, max_concurrent=5)


def _intent(**over: object) -> OrderIntent:
    base: dict[str, object] = dict(
        ticker="AMD", timeframe="1d", play_type="continuation",
        entry_floor=99.0, entry_ceiling=101.0, stop=94.0, target=110.0,
        conviction="high", shares=10, risk_dollars=70.0,
        edge_played="e", key_risk="", insight="i",
        side="long", limit_price=101.0,
    )
    base.update(over)
    return OrderIntent(**base)  # type: ignore[arg-type]


def _session() -> Session:
    return Session(get_engine("sqlite:///:memory:"))


def _live_settings(*, mode: str = "live", allow: bool = True) -> Settings:
    """A Settings snapshot with the execution-mode + allow-real-money locks set as asked.

    Only the two fields ``can_arm_real_money`` reads matter; the rest are filler so the
    frozen dataclass is constructible. Built directly (not via env) so the test controls the
    locks without monkeypatching the environment."""
    from pathlib import Path
    return Settings(
        db_url="sqlite:///:memory:", chart_dir=Path("."), cache_dir=Path("."),
        pdf_dir=Path("."), edge_dir=Path("."), blob_account_url=None, blob_container="c",
        key_vault_url=None,
        azure_client_id=None, acs_endpoint=None, acs_sender=None,
        deep_analysis_enabled=False, analysis_model="m", analysis_reasoning="high",
        deep_analysis_top_n=5, deep_analysis_kinds=frozenset(), analysis_max_searches=4,
        deep_analysis_max_usd=None,
        account_equity=None, risk_per_trade_dollars=None, risk_pct=0.01, max_shares=None,
        execution_mode=mode, max_daily_notional=None, max_daily_loss=None,
        max_concurrent=None, broker="alpaca", allow_real_money=allow,
    )


# ---------------------------------------------------------------------------
# the happy path on a PAPER broker: a submitted_live log + broker_order_id,
# NO position opened, and the real-money guard is NOT consulted.
# ---------------------------------------------------------------------------
def test_paper_broker_submit_records_order_opens_no_position() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=False)
        # gate_ready_fn deliberately raises: a paper broker must never consult it.
        def _boom(_s: Session) -> bool:
            raise AssertionError("real-money guard consulted for a paper broker")

        adapter = LiveAdapter(broker, settings=_live_settings(mode="off", allow=False),
                              gate_ready_fn=_boom)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)

        assert result.status == "submitted_live"
        assert result.account == "live"
        assert result.broker_order_id == "fake-0"
        assert result.trade_id is None              # no position materialized

        # NO PaperTrade opened -- the fill price is unknown at submit.
        assert s.query(PaperTrade).count() == 0

        log = s.query(ExecutionLog).one()
        assert log.status == "submitted_live"
        assert log.account == "live" and log.mode == "live"
        assert log.broker == "fake"
        assert log.broker_order_id == "fake-0"
        assert log.broker_status == "new"
        assert log.side == "long" and log.limit_price == 101.0 and log.shares == 10
        assert log.stop == 94.0 and log.target == 110.0
        assert log.notional == 1010.0             # shares * limit_price

        # the broker really received the order, keyed by our idempotency key.
        assert len(broker.list_open_orders()) == 1


# ---------------------------------------------------------------------------
# idempotency: a duplicate submit places no second broker order + one log row.
# ---------------------------------------------------------------------------
def test_duplicate_submit_no_second_broker_order() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=False)
        adapter = LiveAdapter(broker, settings=_live_settings(), gate_ready_fn=lambda _s: True)
        first = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        second = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)

        # FakeBroker is idempotent on client_order_id (== our key): only ONE broker order.
        assert len(broker.list_open_orders()) == 1
        assert second.broker_order_id == first.broker_order_id == "fake-0"
        # the ExecutionLog unique key collapses the second log to the same single row.
        assert s.query(ExecutionLog).count() == 1
        assert s.query(PaperTrade).count() == 0


# ---------------------------------------------------------------------------
# a blocked hard limit -> skipped, NO broker call, no position.
# ---------------------------------------------------------------------------
def test_blocked_limit_skips_without_calling_broker() -> None:
    with _session() as s:
        # a live position already AT a max_concurrent cap of 1 -> the next is blocked.
        s.add(PaperTrade(ticker="X", timeframe="1d", horizon="medium", account="live",
                         signal_score=0.5, rank=1, fill_status="filled", status="open",
                         stop=9.0, target=12.0, risk=1.0))
        s.commit()
        broker = FakeBroker(real_money=False)
        limits = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=1)
        adapter = LiveAdapter(broker, settings=_live_settings(), gate_ready_fn=lambda _s: True)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=limits)

        assert result.status == "skipped"
        assert "concurrent" in result.detail.lower() or "position" in result.detail.lower()
        # NO broker order was placed and NO new position opened (only the pre-seeded one).
        assert broker.list_open_orders() == []
        assert s.query(PaperTrade).count() == 1
        skipped = s.query(ExecutionLog).filter_by(status="skipped").one()
        assert skipped.ticker == "AMD" and skipped.account == "live"
        assert skipped.broker == "fake"


# ---------------------------------------------------------------------------
# the real-money guard: a real-money broker WITHOUT the locks is rejected,
# no broker order placed.
# ---------------------------------------------------------------------------
def test_real_money_without_gate_is_rejected_no_broker_call() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=True)
        # all locks set EXCEPT the gate -> rejected on the gate.
        adapter = LiveAdapter(broker, settings=_live_settings(mode="live", allow=True),
                              gate_ready_fn=lambda _s: False)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=FULL_CAPS)

        assert result.status == "rejected"
        assert result.account == "live"
        assert "gate" in result.detail.lower()
        assert broker.list_open_orders() == []            # the broker was never touched
        assert s.query(PaperTrade).count() == 0
        rejected = s.query(ExecutionLog).filter_by(status="rejected_live").one()
        assert rejected.account == "live"


def test_real_money_mode_not_live_is_rejected() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=True)
        # the gate is ready but execution_mode != live -> still refused (first failing lock).
        adapter = LiveAdapter(broker, settings=_live_settings(mode="paper", allow=True),
                              gate_ready_fn=lambda _s: True)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=FULL_CAPS)

        assert result.status == "rejected"
        assert "live" in result.detail.lower()
        assert broker.list_open_orders() == []
        assert s.query(ExecutionLog).filter_by(status="rejected_live").count() == 1


# ---------------------------------------------------------------------------
# the real-money guard: ALL three locks AND every cap -> submitted_live.
# ---------------------------------------------------------------------------
def test_real_money_with_all_locks_and_caps_submits() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=True)
        adapter = LiveAdapter(broker, settings=_live_settings(mode="live", allow=True),
                              gate_ready_fn=lambda _s: True)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=FULL_CAPS)

        assert result.status == "submitted_live"
        assert result.broker_order_id == "fake-0"
        assert len(broker.list_open_orders()) == 1
        assert s.query(PaperTrade).count() == 0           # still opens NO position
        log = s.query(ExecutionLog).filter_by(status="submitted_live").one()
        assert log.broker_order_id == "fake-0" and log.broker_status == "new"


# ---------------------------------------------------------------------------
# the real-money guard: locks OK but an UNSET cap -> rejected (real_money_limits_ok).
# ---------------------------------------------------------------------------
def test_real_money_with_unset_cap_is_rejected() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=True)
        adapter = LiveAdapter(broker, settings=_live_settings(mode="live", allow=True),
                              gate_ready_fn=lambda _s: True)
        # all three locks pass, but the caps are unbounded -> refused by the caps mandate.
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)

        assert result.status == "rejected"
        assert "max_daily_notional" in result.detail.lower() or "cap" in result.detail.lower()
        assert broker.list_open_orders() == []
        assert s.query(ExecutionLog).filter_by(status="rejected_live").count() == 1


# ---------------------------------------------------------------------------
# graceful broker errors: a broker that RAISES on submit must not propagate.
# ---------------------------------------------------------------------------
def test_broker_raise_on_submit_is_graceful() -> None:
    class _RaisingBroker(FakeBroker):
        def submit_order(self, spec: BrokerOrderSpec) -> BrokerOrder:
            raise RuntimeError("connection refused")

    with _session() as s:
        broker = _RaisingBroker(real_money=False)
        adapter = LiveAdapter(broker, settings=_live_settings(), gate_ready_fn=lambda _s: True)
        # no exception escapes -- the adapter swallows it into a rejected_live result.
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)

        assert result.status == "rejected"
        assert "broker error" in result.detail.lower()
        assert s.query(PaperTrade).count() == 0
        rejected = s.query(ExecutionLog).filter_by(status="rejected_live").one()
        assert "connection refused" in rejected.detail


# ---------------------------------------------------------------------------
# a broker that returns a rejected order -> rejected (with the broker_order_id).
# ---------------------------------------------------------------------------
def test_broker_returns_rejected_order() -> None:
    class _RejectingBroker(FakeBroker):
        def submit_order(self, spec: BrokerOrderSpec) -> BrokerOrder:
            order = super().submit_order(spec)
            self.reject(order.broker_order_id)
            return self.get_order(order.broker_order_id)

    with _session() as s:
        broker = _RejectingBroker(real_money=False)
        adapter = LiveAdapter(broker, settings=_live_settings(), gate_ready_fn=lambda _s: True)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)

        assert result.status == "rejected"
        assert result.broker_order_id == "fake-0"     # the rejected order's id is carried
        assert s.query(PaperTrade).count() == 0
        rejected = s.query(ExecutionLog).filter_by(status="rejected_live").one()
        assert rejected.account == "live"


def test_adapter_name() -> None:
    assert LiveAdapter(FakeBroker()).name == "live"


# ---------------------------------------------------------------------------
# bracket orders: the venue holds the protective stop + target itself, so a
# filled position stays protected even if the screener dies.
# ---------------------------------------------------------------------------
def test_submit_carries_the_intents_stop_and_target_as_bracket_legs() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=False)
        adapter = LiveAdapter(broker, settings=_live_settings(),
                              gate_ready_fn=lambda _s: True)
        result = adapter.submit(_intent(stop=94.0, target=110.0),
                                session=s, run_date=RUN, limits=NO_LIMITS)

        assert result.status == "submitted_live"
        (spec,) = broker.submitted_specs
        # the deterministic levels ride the spec verbatim (North Star #4: never computed here)
        assert spec.stop_loss == 94.0
        assert spec.take_profit == 110.0


def test_bracket_off_falls_back_to_a_plain_limit_entry() -> None:
    from dataclasses import replace as dc_replace

    with _session() as s:
        broker = FakeBroker(real_money=False)
        settings = dc_replace(_live_settings(), bracket_orders=False)
        adapter = LiveAdapter(broker, settings=settings, gate_ready_fn=lambda _s: True)
        adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)

        (spec,) = broker.submitted_specs
        assert spec.stop_loss is None and spec.take_profit is None

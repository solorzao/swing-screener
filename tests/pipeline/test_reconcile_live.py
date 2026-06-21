"""The LOAD-BEARING live-reconcile integration (``pipeline.reconcile.reconcile_live``).

A live position is filled + closed by the BROKER, never by our bar-stepper. This suite
drives the whole lifecycle through the in-memory ``FakeBroker`` + an in-memory DB:

    LiveAdapter.submit -> reconcile_live (materialize from broker fill) -> reconcile_live
    (exit on a venue close)

and pins down the four load-bearing properties:

* the live PaperTrade is materialized from the BROKER's fill price (``filled_avg_price``),
  NOT the intent's limit price;
* materialization is idempotent -- the ``submitted_live`` -> ``filled_live`` status flip on
  the ExecutionLog is the guard, so a re-poll never opens a second position;
* the exit is reconciled from broker truth (the venue close removes the position) with a
  ``realized_r`` off broker prices + an ``ExitEvent(account="live", is_paper=False)``, and is
  idempotent (a closed live row is never re-closed);
* THE KEYSTONE -- ``advance_open`` NEVER touches a live row, even handed bars that would
  stop/target a simulated trade.

In-memory SQLite, no network.
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db.models import ExecutionLog, ExitEvent, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.broker import FakeBroker
from swing_screener.pipeline.execution import LiveAdapter
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.pipeline.reconcile import reconcile_live
from swing_screener.pipeline.shadow import advance_open
from swing_screener.settings import Limits

RUN = date(2026, 6, 19)
TODAY = date(2026, 6, 20)
NO_LIMITS = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=None)
CFG = StrategyConfig()


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


def _submit(s: Session, broker: FakeBroker, intent: OrderIntent | None = None) -> str:
    """Submit a live order through the LiveAdapter; return its broker_order_id."""
    adapter = LiveAdapter(broker, gate_ready_fn=lambda _s: True)
    result = adapter.submit(intent or _intent(), session=s, run_date=RUN, limits=NO_LIMITS)
    assert result.status == "submitted_live"
    assert result.broker_order_id is not None
    return result.broker_order_id


# ---------------------------------------------------------------------------
# a still-`new` order -> no position yet, the log stays submitted_live.
# ---------------------------------------------------------------------------
def test_unfilled_order_materializes_nothing() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=False)
        _submit(s, broker)  # the broker order is still `new` (unfilled)

        changed = reconcile_live(s, broker, today=TODAY)

        assert changed == 0
        assert s.query(PaperTrade).count() == 0          # nothing materialized
        log = s.query(ExecutionLog).one()
        assert log.status == "submitted_live"            # still pending


# ---------------------------------------------------------------------------
# a filled order -> ONE live PaperTrade at the BROKER's price (not the intent limit),
# the log flips to filled_live; a re-poll does NOT double-materialize.
# ---------------------------------------------------------------------------
def test_fill_materializes_live_trade_from_broker_price_idempotently() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=False)
        oid = _submit(s, broker)
        # the BROKER fills at 99.5 -- DELIBERATELY != the intent's 101.0 limit.
        broker.fill(oid, price=99.5)

        changed = reconcile_live(s, broker, today=TODAY)
        assert changed == 1

        pt = s.query(PaperTrade).one()
        assert pt.account == "live"
        assert pt.status == "open" and pt.fill_status == "filled"
        # the load-bearing assertion: entry is the BROKER fill, NOT the intent's 101.0 limit.
        assert pt.entry_price == 99.5
        assert pt.risk == 99.5 - 94.0                    # entry - stop (stop from the log)
        assert pt.stop == 94.0 and pt.target == 110.0
        assert pt.entry_date == TODAY and pt.opened_date == TODAY
        assert pt.hold_bars == 0 and pt.remaining_frac == 1.0
        assert pt.partial_done is False and pt.high_water == 99.5

        log = s.query(ExecutionLog).one()
        assert log.status == "filled_live"               # the idempotency guard flipped
        assert log.broker_status == "filled"

        # RE-POLL: the same broker state must NOT open a second position.
        again = reconcile_live(s, broker, today=TODAY)
        assert again == 0
        assert s.query(PaperTrade).count() == 1


# ---------------------------------------------------------------------------
# a partially-filled order (filled_qty > 0) also materializes from the broker fill.
# ---------------------------------------------------------------------------
def test_partial_fill_materializes_from_broker_price() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=False)
        oid = _submit(s, broker)
        broker.partially_fill(oid, price=100.25, qty=4)  # filled_qty > 0

        changed = reconcile_live(s, broker, today=TODAY)
        assert changed == 1
        pt = s.query(PaperTrade).one()
        assert pt.account == "live" and pt.entry_price == 100.25
        assert pt.risk == 100.25 - 94.0
        log = s.query(ExecutionLog).one()
        assert log.status == "filled_live" and log.broker_status == "partially_filled"


# ---------------------------------------------------------------------------
# a venue close -> the live trade is closed at the BROKER's exit price with realized_r
# from broker prices + an ExitEvent(account="live", is_paper=False); a re-poll is a no-op.
# ---------------------------------------------------------------------------
def test_venue_close_reconciles_exit_idempotently() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=False)
        oid = _submit(s, broker)
        broker.fill(oid, price=99.5)
        reconcile_live(s, broker, today=TODAY)        # materialize the open live trade

        # the venue closes the position AT 104.0 (scripted broker exit price).
        broker.close_position("AMD", price=104.0)
        changed = reconcile_live(s, broker, today=date(2026, 6, 25))
        assert changed == 1

        pt = s.query(PaperTrade).one()
        assert pt.status == "closed"
        assert pt.exit_price == 104.0
        assert pt.exit_reason == "broker_close"
        assert pt.exit_date == date(2026, 6, 25)
        # realized_r from BROKER prices: (104.0 - 99.5) / (99.5 - 94.0)
        assert pt.realized_r == (104.0 - 99.5) / (99.5 - 94.0)

        event = s.query(ExitEvent).one()
        assert event.account == "live"
        assert event.is_paper is False
        assert event.trade_id == pt.id

        # RE-POLL: a closed live row is never re-closed, no second ExitEvent.
        again = reconcile_live(s, broker, today=date(2026, 6, 25))
        assert again == 0
        assert s.query(ExitEvent).count() == 1


# ---------------------------------------------------------------------------
# a canceled order -> the log flips to canceled, no position.
# ---------------------------------------------------------------------------
def test_canceled_order_flips_log_no_position() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=False)
        oid = _submit(s, broker)
        broker.cancel_order(oid)

        changed = reconcile_live(s, broker, today=TODAY)
        assert changed == 1
        assert s.query(PaperTrade).count() == 0
        log = s.query(ExecutionLog).one()
        assert log.status == "canceled"


# ---------------------------------------------------------------------------
# a rejected order -> the log flips to rejected_live, no position.
# ---------------------------------------------------------------------------
def test_rejected_order_flips_log_no_position() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=False)
        oid = _submit(s, broker)
        broker.reject(oid)

        changed = reconcile_live(s, broker, today=TODAY)
        assert changed == 1
        assert s.query(PaperTrade).count() == 0
        assert s.query(ExecutionLog).one().status == "rejected_live"


# ---------------------------------------------------------------------------
# a non-positive risk (limit/fill at or below the stop) -> skip, no position,
# the log is NOT flipped to filled_live (so it isn't lost; re-polled next cycle).
# ---------------------------------------------------------------------------
def test_nonpositive_risk_fill_is_skipped_not_materialized() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=False)
        # stop ABOVE the fill -> entry - stop <= 0 -> can't honestly book it.
        oid = _submit(s, broker, _intent(stop=100.0))
        broker.fill(oid, price=99.5)

        changed = reconcile_live(s, broker, today=TODAY)
        assert changed == 0
        assert s.query(PaperTrade).count() == 0
        # the log is left submitted_live (not materialized) rather than silently lost.
        assert s.query(ExecutionLog).one().status == "submitted_live"


# ---------------------------------------------------------------------------
# THE KEYSTONE: advance_open NEVER touches the live row, even with bars that WOULD
# stop AND target a simulated trade. reconcile_live exclusively owns the live book.
# ---------------------------------------------------------------------------
def test_advance_open_never_touches_the_live_row() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=False)
        oid = _submit(s, broker)
        broker.fill(oid, price=99.5)
        reconcile_live(s, broker, today=TODAY)
        pt = s.query(PaperTrade).one()
        assert pt.status == "open" and pt.entry_price == 99.5 and pt.hold_bars == 0

        # a bar that would BOTH breach the stop (low 90 < 94) AND hit the target
        # (high 120 > 110) for a simulated trade -- advance_open must IGNORE the live row.
        violent = {"low": 90.0, "high": 120.0, "close": 95.0,
                   "shaved_head": False, "bearish": True, "atr": 2.0}
        advance_open(s, {("AMD", "1d"): violent}, CFG, today=date(2026, 6, 23))

        live = s.query(PaperTrade).one()
        assert live.status == "open"                 # untouched: NOT stopped, NOT targeted
        assert live.entry_price == 99.5
        assert live.hold_bars == 0                   # never advanced
        assert live.exit_price is None and live.realized_r is None
        assert live.last_advanced is None
        # and the live exit is still reconcile's to own: a venue close closes it.
        broker.close_position("AMD", price=104.0)
        reconcile_live(s, broker, today=date(2026, 6, 24))
        assert s.query(PaperTrade).one().status == "closed"

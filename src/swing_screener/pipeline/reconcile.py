"""The live-reconcile pass: the broker owns the fills, we reconcile -- never simulate.

``reconcile_live`` is the polling sibling of the bar-stepper (``shadow.advance_open``), but
for the ``account="live"`` book ONLY. The two engines are deliberately DISJOINT: the stepper
excludes ``account="live"`` (``repo.load_open_paper_trades(exclude_live=True)``), so a live
position is filled, trailed, and closed by the BROKER and materialized/closed here from broker
truth -- never invented by a simulated fill. It does two things each cycle:

1. **Materialize fills.** For every ``submitted_live`` ExecutionLog (a working order not yet
   materialized), read the broker order:

   * ``filled`` (or ``partially_filled`` with ``filled_qty > 0``) -> open ONE ``account="live"``
     ``PaperTrade`` from the BROKER's fill: ``entry_price = order.filled_avg_price`` (NOT the
     intent's limit), ``qty = order.filled_qty`` (venue truth, NOT the ticket's requested
     shares), ``risk = entry_price - stop`` (the ``stop`` recorded on the ExecutionLog;
     a non-positive risk is logged + skipped, never booked). The runner-state fields mirror a
     fresh shadow/paper fill (``hold_bars=0``, ``remaining_frac=1.0``, ``partial_done=False``,
     ``high_water=entry_price``) so the row is shaped like any open trade. Then flip the
     ExecutionLog ``status="filled_live"`` + ``broker_status``.
   * ``canceled`` -> ExecutionLog ``status="canceled"``; ``rejected`` -> ``status="rejected_live"``.
     No position either way.

   **Idempotency guard:** the ``submitted_live`` -> ``filled_live`` status transition. A re-poll
   only loads ``submitted_live`` rows, so a fill is materialized EXACTLY ONCE -- the flipped log
   is no longer a candidate. (A non-positive-risk fill is intentionally NOT flipped: it stays
   ``submitted_live`` so it is re-evaluated next cycle rather than silently lost.)

2. **Reconcile exits.** For every OPEN ``account="live"`` PaperTrade, check the broker's
   positions: if there is NO ``BrokerPosition`` for its ticker, the venue closed it -> write the
   exit from broker truth: ``exit_price`` from ``broker.last_close_price(ticker)`` (the broker
   owns the price -- see below), ``exit_date = today``, ``exit_reason = "broker_close"``,
   ``realized_r = (exit_price - entry_price) / risk``, ``status="closed"``, plus an
   ``ExitEvent(account="live", is_paper=False)``. **Idempotent:** only ``status="open"`` live
   trades are checked (``repo.load_open_live_trades``), so a closed row is never re-closed.

The whole pass is **idempotent on re-poll**: running it twice against the same broker state makes
no further change (the status-transition guard for fills, the open-only loader for exits).

EXIT-PRICE SOURCING (a documented choice): a flat broker position carries no price, so the exit
price is read from ``broker.last_close_price(ticker)`` -- the broker's reported close. The real
Alpaca client (Task 6) derives that from the closing order's ``filled_avg_price``; ``FakeBroker``
scripts it via ``close_position(symbol, price=...)``. If the broker reports no close price
(``None``), the trade is left OPEN and re-polled next cycle rather than closed at a guessed level
-- never simulate a price the broker did not give us.

``today`` is THREADED IN (``reconcile_live(session, broker, *, today)``) so every materialized
``entry_date``/``opened_date`` and reconciled ``exit_date`` is deterministic, never a hidden
``date.today()``.
"""

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import ExecutionLog, PaperTrade
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.broker import BrokerClient
from swing_screener.pipeline.variants import DEFAULT_VARIANT

logger = logging.getLogger(__name__)

LIVE_ACCOUNT = "live"
# the broker order states that mean "we have a real fill to materialize". A partial counts
# only when something actually filled (filled_qty > 0); a bare `partially_filled` with zero
# filled qty is still pending.
_FILLED_STATUSES = ("filled", "partially_filled")


def reconcile_live(session: Session, broker: BrokerClient, *, today: date) -> int:
    """Poll the broker; materialize live fills + reconcile live exits. Return the change count.

    Idempotent on re-poll (see the module docstring). ``today`` is threaded in so every
    materialized/closed date is deterministic. Commits once at the end."""
    changed = _materialize_fills(session, broker, today=today)
    changed += _reconcile_exits(session, broker, today=today)
    session.commit()
    return changed


def _materialize_fills(session: Session, broker: BrokerClient, *, today: date) -> int:
    """Turn each ``submitted_live`` ExecutionLog into a live PaperTrade (or flip a terminal
    broker state onto the log). The ``submitted_live`` -> ``filled_live`` flip is the
    materialize-exactly-once guard. No commit (the caller commits)."""
    pending = session.scalars(
        select(ExecutionLog).where(ExecutionLog.status == "submitted_live")
    ).all()
    changed = 0
    for log in pending:
        if log.broker_order_id is None:
            continue  # a submitted_live row always carries the id; defensive skip.
        order = broker.get_order(log.broker_order_id)

        if order.status in _FILLED_STATUSES and order.filled_qty > 0:
            # NOTE (Phase-4 scope): a `partially_filled` order is materialized ONCE at its
            # filled_avg_price with qty = the broker's filled_qty (venue truth, stamped
            # below), and the log is flipped out of submitted_live. The remaining limitation
            # is only that the residual (unfilled) shares are NOT later reconciled if the
            # venue fills more -- harmless in practice, since whole-share Alpaca-paper fills
            # are effectively atomic. Real-money endpoints partially fill for real -- a stale
            # qty understates $ exposure/loss, so residual reconciliation must land before
            # this books against a real-money endpoint.
            if order.filled_avg_price is None:
                continue  # filled but no price yet -> re-poll next cycle, never guess.
            entry_price = order.filled_avg_price
            risk = entry_price - log.stop
            if risk <= 0:
                # The broker filled at/above the stop -> we can't honestly book it (the
                # realized-R division needs a strictly-positive risk). Log + SKIP, and
                # leave the log submitted_live so it isn't silently lost.
                logger.warning(
                    "live fill for %s (order %s) has non-positive risk "
                    "(entry %.4f <= stop %.4f); skipping materialization",
                    log.ticker, log.broker_order_id, entry_price, log.stop,
                )
                continue
            session.add(_materialized_trade(
                log, entry_price=entry_price, risk=risk, today=today,
                # Venue truth wins: the broker's filled_qty, not the ticket's requested
                # shares. The log.shares fallback is purely defensive -- this branch is
                # already guarded by filled_qty > 0, so in practice filled_qty always wins.
                qty=int(order.filled_qty) if order.filled_qty > 0 else int(log.shares),
            ))
            # The idempotency guard: flip the log out of `submitted_live` so a re-poll never
            # re-materializes this fill. `broker_status` is stamped once here (a point-in-time
            # submit/fill record); the live PaperTrade -- not this log -- tracks the position
            # from here, so later broker status changes short of a close aren't re-written.
            log.status = "filled_live"
            log.broker_status = order.status
            changed += 1
        elif order.status == "canceled":
            log.status = "canceled"
            log.broker_status = order.status
            changed += 1
        elif order.status == "rejected":
            log.status = "rejected_live"
            log.broker_status = order.status
            changed += 1
        # else: still `new` / a zero-qty `partially_filled` -> pending, re-polled next cycle.
    return changed


def _materialized_trade(
    log: ExecutionLog, *, entry_price: float, risk: float, today: date, qty: int
) -> PaperTrade:
    """Build the ``account="live"`` open PaperTrade from the broker fill + the ExecutionLog spec.

    Mirrors a freshly-filled shadow/paper open (PaperAdapter / shadow.open_from_signals):
    concrete entry + strictly-positive risk, ``status="open"``, ``hold_bars=0``, and the
    runner-state defaults (``remaining_frac=1.0``, ``partial_done=False``, ``high_water=entry``)
    seeded so the row is shaped like every other open trade. The levels come from the BROKER
    (entry) + the deterministic ExecutionLog (``stop``/``target``/pick keys), and the SIZE
    (``qty``) is the broker's filled share count -- what realized-$ math multiplies by.
    Never recomputed."""
    return PaperTrade(
        account=LIVE_ACCOUNT,
        arm=BASELINE,
        variant=DEFAULT_VARIANT,
        ticker=log.ticker,
        timeframe=log.timeframe,
        horizon="",
        play_type=log.play_type,
        signal_id=None,
        signal_score=0.0,
        rank=0,
        fill_status="filled",
        status="open",
        entry_price=entry_price,
        entry_date=today,
        opened_date=today,
        stop=log.stop,
        target=log.target,
        risk=risk,
        qty=qty,
        hold_bars=0,
        remaining_frac=1.0,
        partial_done=False,
        high_water=entry_price,
    )


def _reconcile_exits(session: Session, broker: BrokerClient, *, today: date) -> int:
    """Close each OPEN live trade whose broker position is gone (a venue-side exit), from broker
    truth. Idempotent: only ``status="open"`` live trades are loaded, so a closed row is never
    re-closed. No commit (the caller commits)."""
    open_symbols = {p.symbol for p in broker.get_positions()}
    changed = 0
    for pt in repo.load_open_live_trades(session):
        if pt.ticker in open_symbols:
            continue  # still held at the venue -> nothing to reconcile.
        exit_price = broker.last_close_price(pt.ticker)
        if exit_price is None:
            # The venue position is gone but the broker reports no close price -> leave it OPEN
            # and re-poll next cycle rather than close at a guessed level. Never simulate.
            logger.warning(
                "live position %s is gone at the venue but the broker reports no close "
                "price; leaving the trade open for the next reconcile", pt.ticker,
            )
            continue
        # Open live trades always carry a concrete entry + strictly-positive risk (materialized
        # that way), so the realized-R division is safe.
        assert pt.entry_price is not None and pt.risk is not None
        pt.status = "closed"
        pt.exit_price = exit_price
        pt.exit_reason = "broker_close"
        pt.exit_date = today
        pt.realized_r = (exit_price - pt.entry_price) / pt.risk
        repo.record_exit_event(
            session,
            is_paper=False,
            account=LIVE_ACCOUNT,
            trade_id=pt.id,
            tier="",
            reason="broker_close",
            message=f"{pt.ticker} {pt.timeframe} broker_close @ {exit_price}",
            created_date=today,
        )
        changed += 1
    return changed

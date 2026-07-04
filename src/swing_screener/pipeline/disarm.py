"""The DISARM runbook step, as a command: pull entry-side orders, keep the stops.

Flipping ``SWING_EXECUTION_MODE`` off stops NEW submits (the kill switch re-reads the
mode mid-dispatch), but it never touched what was already at the venue -- resting entry
limits could still fill AFTER a disarm, growing exposure precisely when the operator was
trying to reduce it (2026-07 review). This command closes that gap:

    python -m swing_screener.pipeline.disarm [--dry-run]

Since brackets (PR #92) the venue also holds each position's protective STOP leg, so
disarm cancels ONLY entry-side (buy) orders -- a blanket cancel would strip the stops
off the very positions it deliberately does NOT close, handing the operator an
unprotected book at the moment of maximum stress (2026-07-04 review). Every remaining
position must end the disarm stop-protected: a dead stop leg is re-submitted as a plain
GTC stop at the ExecutionLog ticket's RECORDED level (copied, never computed -- North
Star #4); with no recorded level the position is reported UNPROTECTED, loudly, and left
to the human. Positions are deliberately NOT auto-closed -- liquidation is a human
decision (North Star #3); the screen run keeps reconciling existing live exposure even
when the mode is off. ``--dry-run`` lists what would be cancelled / re-submitted without
touching the venue.
"""

import argparse
import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.session import get_engine
from swing_screener.pipeline.broker import BrokerClient, BrokerOrder, BrokerOrderSpec
from swing_screener.pipeline.broker_alpaca import build_broker
from swing_screener.settings import Settings, load_settings

log = logging.getLogger(__name__)

#: order types that count as a live protective stop for a long position.
_STOP_TYPES = ("stop", "stop_limit")


def pull_entry_orders(
    broker: BrokerClient, *, dry_run: bool = False
) -> tuple[list[BrokerOrder], list[BrokerOrder]]:
    """Cancel every resting ENTRY-side (buy) order; sell-side orders stay working.

    The sell side is the protection: bracket stop legs (and target legs) of FILLED
    positions are open sell orders, and cancelling them would leave the position
    unguarded. Returns ``(entry_orders, sell_orders)`` as observed before any cancel."""
    open_orders = broker.list_open_orders()
    entries = [o for o in open_orders if o.side == "buy"]
    sells = [o for o in open_orders if o.side != "buy"]
    for order in entries:
        if dry_run:
            log.info("[dry-run] would cancel entry order: %s (broker id %s)",
                     order.symbol, order.broker_order_id)
        else:
            broker.cancel_order(order.broker_order_id)
            log.info("cancelled entry order: %s (broker id %s)",
                     order.symbol, order.broker_order_id)
    for order in sells:
        log.info("left working (protective/exit side): %s %s (broker id %s)",
                 order.symbol, order.order_type, order.broker_order_id)
    return entries, sells


def ensure_stop_protection(
    broker: BrokerClient,
    stop_for: Callable[[str], float | None],
    *,
    key_suffix: str,
    dry_run: bool = False,
) -> list[str]:
    """Every open position must retain a live protective sell STOP.

    A position whose stop leg died (e.g. a pre-fix blanket disarm) gets a plain GTC
    stop re-submitted at ``stop_for(symbol)`` -- the ticket's recorded level, COPIED,
    never computed. When no level exists the position is reported UNPROTECTED and left
    to the human (guessing a level would violate North Star #4). Returns the symbols
    left unprotected."""
    protected = {o.symbol for o in broker.list_open_orders()
                 if o.side == "sell" and o.order_type in _STOP_TYPES}
    unprotected: list[str] = []
    for pos in broker.get_positions():
        if pos.symbol in protected:
            continue
        stop = stop_for(pos.symbol)
        if stop is None:
            unprotected.append(pos.symbol)
            log.error("UNPROTECTED position %s x%d: no live stop at the venue and no "
                      "recorded ticket level to restore -- protect it manually NOW",
                      pos.symbol, pos.qty)
            continue
        if dry_run:
            log.info("[dry-run] would re-submit protective stop: %s x%d @ %.2f",
                     pos.symbol, pos.qty, stop)
            continue
        broker.submit_order(BrokerOrderSpec(
            client_order_id=f"disarm-stop-{pos.symbol}-{key_suffix}",
            symbol=pos.symbol, side="sell", qty=pos.qty, order_type="stop",
            limit_price=None, time_in_force="gtc", stop_price=stop))
        log.info("re-submitted protective stop: %s x%d @ %.2f (level copied from the "
                 "ExecutionLog ticket)", pos.symbol, pos.qty, stop)
    return unprotected


def _recorded_stop_lookup(settings: Settings) -> Callable[[str], float | None]:
    """A per-symbol lookup of the newest live ticket's recorded stop.

    When the DB is unreachable the lookup degrades to always-None with a loud error --
    disarm still pulls the entry orders (the urgent half) and reports what it could
    not protect, rather than dying before touching the venue."""
    try:
        engine = get_engine(settings.db_url)
    except Exception:
        log.error("DB unreachable (%s): dead stop legs cannot be restored from the "
                  "ExecutionLog tickets", settings.db_url, exc_info=True)
        return lambda symbol: None

    def lookup(symbol: str) -> float | None:
        with Session(engine) as session:
            return repo.latest_recorded_stop(session, symbol)

    return lookup


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cancel entry-side resting orders at the configured broker (the "
                    "disarm runbook step) and verify every remaining position keeps a "
                    "protective stop. Positions are reported, never auto-closed.")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be cancelled / re-submitted without "
                             "touching the venue")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    settings = load_settings()
    broker = build_broker(settings)
    if broker is None:
        raise SystemExit("no broker configured (SWING_BROKER is unset) -- nothing to disarm")

    entries, _ = pull_entry_orders(broker, dry_run=args.dry_run)
    if not entries:
        log.info("no resting entry orders to cancel")

    positions = broker.get_positions()
    unprotected: list[str] = []
    if positions:
        unprotected = ensure_stop_protection(
            broker, _recorded_stop_lookup(settings),
            key_suffix=datetime.now().strftime("%Y%m%d%H%M%S"), dry_run=args.dry_run)
    for pos in positions:
        log.info("OPEN POSITION (not auto-closed): %s x%d @ %.2f",
                 pos.symbol, pos.qty, pos.avg_entry_price)
    if positions:
        log.info("%d open position(s) remain at the venue -- closing them is a human "
                 "decision; the screen run keeps reconciling them while disarmed",
                 len(positions))
    if unprotected:
        log.error("%d position(s) left UNPROTECTED: %s",
                  len(unprotected), ", ".join(unprotected))


if __name__ == "__main__":
    main()

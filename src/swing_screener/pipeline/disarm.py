"""The DISARM runbook step, as a command: pull every resting order at the venue.

Flipping ``SWING_EXECUTION_MODE`` off stops NEW submits (the kill switch re-reads the
mode mid-dispatch), but it never touched what was already at the venue -- resting entry
limits could still fill AFTER a disarm, growing exposure precisely when the operator was
trying to reduce it (2026-07 review). This command closes that gap:

    python -m swing_screener.pipeline.disarm [--dry-run]

It cancels ALL open orders via the configured broker and reports the open positions that
remain. Positions are deliberately NOT auto-closed -- liquidation is a human decision
(North Star #3); the screen run keeps reconciling existing live exposure even when the
mode is off, so the remaining positions stay tracked. ``--dry-run`` lists what would be
cancelled without touching the venue.
"""

import argparse
import logging

from swing_screener.pipeline.broker_alpaca import build_broker
from swing_screener.settings import load_settings

log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cancel every resting order at the configured broker (the disarm "
                    "runbook step). Open positions are reported, never auto-closed.")
    parser.add_argument("--dry-run", action="store_true",
                        help="list open orders/positions without cancelling anything")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    settings = load_settings()
    broker = build_broker(settings)
    if broker is None:
        raise SystemExit("no broker configured (SWING_BROKER is unset) -- nothing to disarm")

    open_orders = broker.list_open_orders()
    for order in open_orders:
        log.info("resting order: %s %s (broker id %s)", order.symbol, order.status,
                 order.broker_order_id)
    if args.dry_run:
        log.info("[dry-run] %d resting order(s) would be cancelled", len(open_orders))
    elif open_orders:
        broker.cancel_all_orders()
        log.info("cancelled %d resting order(s)", len(open_orders))
    else:
        log.info("no resting orders to cancel")

    positions = broker.get_positions()
    for pos in positions:
        log.info("OPEN POSITION (not auto-closed): %s x%d @ %.2f",
                 pos.symbol, pos.qty, pos.avg_entry_price)
    if positions:
        log.info("%d open position(s) remain at the venue -- closing them is a human "
                 "decision; the screen run keeps reconciling them while disarmed",
                 len(positions))


if __name__ == "__main__":
    main()

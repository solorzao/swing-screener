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
from swing_screener.pipeline.broker import (
    OPEN_STATUSES,
    BrokerClient,
    BrokerOrder,
    BrokerOrderSpec,
    broker_error_detail,
)
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


def _already_working_at_the_venue(broker: BrokerClient, client_order_id: str) -> bool:
    """True when the venue already holds a WORKING order under ``client_order_id``.

    The benign half of a failed stop re-submit. A duplicate client_order_id is
    REJECTED by Alpaca -- it raises -- which is precisely what the day/trip-stamped key
    is FOR: a same-evening (or same-trip) re-run must be a NO-OP, not an abort, and not
    a false UNPROTECTED alarm about a position that is in fact guarded.

    Detected by ASKING THE VENUE, never by parsing the error text: Alpaca's
    duplicate-id message is not reliably parseable, the same reason the LiveAdapter's
    orphan adoption looks the order up instead (``execution._adopt_orphan``). Anything
    less than certain returns False -- a lookup that itself fails, an unknown key, or an
    order that is no longer working (canceled/rejected/filled protects nothing) -- so
    the caller falls through to the honest error path. Absence of evidence is never
    evidence of protection."""
    try:
        existing = broker.get_order_by_client_id(client_order_id)
    except Exception:  # noqa: BLE001 -- a venue boundary: fall through to the error path
        log.warning("could not ask the venue whether %s already exists; the failed "
                    "re-submit is reported as unprotected", client_order_id,
                    exc_info=True)
        return False
    return existing is not None and existing.status in OPEN_STATUSES


def ensure_stop_protection(
    broker: BrokerClient,
    stop_for: Callable[[str], float | None],
    *,
    key_suffix: str,
    dry_run: bool = False,
) -> tuple[list[str], list[str]]:
    """Every open position must retain a live protective sell STOP.

    A position whose stop leg died (e.g. a pre-fix blanket disarm) gets a plain GTC
    stop re-submitted at ``stop_for(symbol)`` -- the ticket's recorded level, COPIED,
    never computed. When no level exists the position is reported UNPROTECTED and left
    to the human (guessing a level would violate North Star #4). Returns
    ``(restored, unprotected)``: ``restored`` names every symbol a stop was
    re-submitted for -- dry-run INCLUDED (the symbols a real run WOULD protect, so a
    hold preview can show them); ``unprotected`` the positions left to the human.

    PER-POSITION ERROR BOUNDARY (2026-07-25 spec review): each submit is isolated, so
    ONE failure can never abort the pass and silently leave the REMAINING positions
    neither restored nor reported -- the loop's whole purpose is the report. A failed
    submit is triaged: a duplicate client_order_id (the venue rejecting our own
    same-key re-run -- see ``_already_working_at_the_venue``) is BENIGN and skipped
    quietly, since that key's stop is the protection; anything else appends the symbol
    to ``unprotected`` with a class-name-only reason and the walk CONTINUES. This makes
    the ``key_suffix`` idempotency claim true against a REAL venue: Alpaca RAISES on a
    duplicate id rather than collapsing to the existing order the way ``FakeBroker``
    does, so before this boundary a same-evening re-run aborted at the first position.

    LAST-INSTANT RE-CHECK (2026-07-18 red-team): open orders are re-listed
    immediately before EACH submit, because a CONCURRENT sweep (a guardrail trip
    sweep racing the cockpit's /api/disarm) carries a different client_order_id
    suffix -- the venue's duplicate-ID rejection cannot collapse that race, and
    two live GTC sell stops on a margin account mean the position is closed and
    then SHORTED. The re-list shrinks the cross-process window to near zero for
    every caller; a symbol protected since the first scan is skipped (and NOT
    reported as restored -- the rival's stop is the protection)."""
    protected = {o.symbol for o in broker.list_open_orders()
                 if o.side == "sell" and o.order_type in _STOP_TYPES}
    restored: list[str] = []
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
            restored.append(pos.symbol)
            log.info("[dry-run] would re-submit protective stop: %s x%d @ %.2f",
                     pos.symbol, pos.qty, stop)
            continue
        fresh_protected = {o.symbol for o in broker.list_open_orders()
                           if o.side == "sell" and o.order_type in _STOP_TYPES}
        if pos.symbol in fresh_protected:
            log.info("protective stop for %s appeared at the venue since the scan "
                     "(a concurrent sweep) -- skipping the re-submit", pos.symbol)
            continue
        client_order_id = f"disarm-stop-{pos.symbol}-{key_suffix}"
        try:
            broker.submit_order(BrokerOrderSpec(
                client_order_id=client_order_id,
                symbol=pos.symbol, side="sell", qty=pos.qty, order_type="stop",
                limit_price=None, time_in_force="gtc", stop_price=stop))
        except Exception as e:  # noqa: BLE001 -- a venue boundary: never abort the pass
            if _already_working_at_the_venue(broker, client_order_id):
                log.info("protective stop for %s already working at the venue under "
                         "%s (duplicate-id rejection) -- the position is protected; "
                         "nothing to do", pos.symbol, client_order_id)
                continue
            unprotected.append(pos.symbol)
            log.error("UNPROTECTED position %s x%d: the protective stop re-submit "
                      "failed (%s) -- protect it manually NOW", pos.symbol, pos.qty,
                      broker_error_detail(e), exc_info=True)
            continue
        restored.append(pos.symbol)
        log.info("re-submitted protective stop: %s x%d @ %.2f (level copied from the "
                 "ExecutionLog ticket)", pos.symbol, pos.qty, stop)
    return restored, unprotected


def run_protective_sweep(
    broker: BrokerClient,
    stop_for: Callable[[str], float | None],
    *,
    key_suffix: str,
) -> tuple[list[BrokerOrder], list[str], list[str]]:
    """The ONE venue-moving sweep body every emergency path shares -- the
    guardrail trip response, the mid-dispatch kill switch, and the manual-HALT
    brake: pull every resting ENTRY-side order (the sell side is the
    protection), then make sure every remaining position keeps a live protective
    stop. Returns ``(entries, restored, unprotected)``: the entry orders
    cancelled, the symbols a stop was re-submitted for, and the positions left
    to the human. Idempotent per ``key_suffix`` (re-runs collapse to the same
    client_order_ids at the venue); callers own the audit trail (DisarmEvent /
    sweep outcome) and the error boundary."""
    entries, _sells = pull_entry_orders(broker)
    restored, unprotected = ensure_stop_protection(broker, stop_for, key_suffix=key_suffix)
    return entries, restored, unprotected


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
    restored: list[str] = []
    unprotected: list[str] = []
    if positions:
        restored, unprotected = ensure_stop_protection(
            broker, _recorded_stop_lookup(settings),
            key_suffix=datetime.now().strftime("%Y%m%d%H%M%S"), dry_run=args.dry_run)
    for pos in positions:
        log.info("OPEN POSITION (not auto-closed): %s x%d @ %.2f",
                 pos.symbol, pos.qty, pos.avg_entry_price)
    if positions:
        log.info("%d open position(s) remain at the venue -- closing them is a human "
                 "decision; the screen run keeps reconciling them while disarmed",
                 len(positions))
    if restored:
        log.info("%d protective stop(s) %s: %s", len(restored),
                 "would be re-submitted" if args.dry_run else "re-submitted",
                 ", ".join(restored))
    if unprotected:
        log.error("%d position(s) left UNPROTECTED: %s",
                  len(unprotected), ", ".join(unprotected))


if __name__ == "__main__":
    main()

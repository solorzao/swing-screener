"""The execution adapters: the seam between an ``OrderIntent`` and the outside world.

Phase 3 turns the insight engine's intents into ACTION through a small set of
pluggable adapters behind a single :class:`ExecutionAdapter` protocol:

* :class:`NoOpAdapter` (``name="off"``) -- the DEFAULT. Writes nothing, opens
  nothing, returns ``skipped``. Exactly today's behavior; the screener never arms
  by accident.
* :class:`ManualAdapter` (``name="manual"``) -- RECORDS an order ticket to the
  ``execution_logs`` audit table for the human to place by hand. Money never moves
  here: no paper position is opened, no broker is called.

The load-bearing safety lives in two places, both INSIDE ``submit`` (never trusting
the caller):

1. The hard-limit clamp (:func:`_limit_block`): before recording anything, ``submit``
   re-checks the per-day notional cap, the max-concurrent-position cap, and the
   per-day realized-loss circuit breaker against the database. A breach is CLAMPED by
   logging a ``skipped`` row (audit) and returning -- the order is never recorded. Each
   check is skipped when its cap is ``None`` (the unbounded sentinel).
2. The idempotency key (:func:`idempotency_key`): unique per ``(pick, run, side)`` so a
   force-resent or hourly-digest re-run that tries to record the SAME order hits the
   ``execution_logs`` unique constraint and is a no-op (``add_execution_log`` returns the
   existing row). One order per pick per run.

PER-DAY-LOSS UNIT DECISION: ``Limits.max_daily_loss`` is interpreted as an **R
threshold**, NOT a dollar amount. The design left $ vs R open; we choose R because the
shadow book's realized outcome is recorded in R (``PaperTrade.realized_r``), so an R
breaker needs no equity figure or $-conversion and stays consistent whether or not
sizing is configured. The breaker trips when the day's summed realized R for the
account is ``<= -max_daily_loss`` (e.g. ``max_daily_loss=2.0`` blocks new orders once
the account has realized -2R or worse today).

MANUAL ACCOUNT: the manual adapter books under ``account="manual"`` -- deliberately
NOT ``"paper"`` (which the shadow book / a future curated intent book own) and not
``"research"`` (the auto-booked grid). A recorded ticket touches no money and opens no
position, so it must not be mistaken for a paper fill; its own account keeps its
limit sums (notional / open-count / realized-loss) cleanly separate.
"""

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from sqlalchemy.orm import Session

from swing_screener.db.repo import (
    add_execution_log,
    count_open_positions,
    execution_logs_for_day,
    realized_r_on,
)
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.settings import Limits

# the account the manual adapter books tickets under -- NOT "paper" / "research".
MANUAL_ACCOUNT = "manual"
# the NoOp adapter touches no book; it reports the research account purely as a label.
OFF_ACCOUNT = "research"


@dataclass(frozen=True)
class OrderResult:
    """The outcome of one adapter ``submit``: the status + the audit detail.

    ``status`` is one of ``recorded`` / ``filled_paper`` / ``skipped`` / ``rejected``.
    ``trade_id`` / ``broker_order_id`` stay None for the manual + off adapters (no
    position opens, no broker is called); a later paper/live adapter fills them in."""

    status: str
    account: str
    detail: str
    trade_id: int | None = None
    broker_order_id: str | None = None


class ExecutionAdapter(Protocol):
    """The pluggable execution seam: turn one ``OrderIntent`` into an ``OrderResult``.

    Every adapter enforces the hard limits INSIDE ``submit`` from the passed ``limits``
    -- never trusting any caller-side gate -- and is idempotent per ``(pick, run, side)``."""

    name: str

    def submit(
        self, intent: OrderIntent, *, session: Session, run_date: date, limits: Limits
    ) -> OrderResult: ...


def idempotency_key(intent: OrderIntent, run_date: date) -> str:
    """Deterministic sha1 hex key, unique per ``(ticker, timeframe, play_type, run, side)``.

    Mirrors ``notify.run._exit_alert_key``: one order per pick per run. A re-submit of
    the same intent on the same run maps to the same key, so ``add_execution_log``'s
    unique constraint collapses it to a no-op. The 40-char hexdigest fits
    ``ExecutionLog.idempotency_key`` (String(64))."""
    raw = (
        f"{intent.ticker}|{intent.timeframe}|{intent.play_type}"
        f"|{run_date.isoformat()}|{intent.side}"
    )
    return hashlib.sha1(raw.encode()).hexdigest()


def notional(intent: OrderIntent) -> float:
    """The dollar notional of an intent: ``shares * limit_price``."""
    return intent.shares * intent.limit_price


def _limit_block(
    session: Session, intent: OrderIntent, *, run_date: date, account: str, limits: Limits
) -> str | None:
    """Return a human reason string if this intent WOULD breach a hard limit, else None.

    A pure READ over the database: the caller (``submit``) does the actual clamp (skip +
    log) -- this only decides. Each check is skipped when its cap is ``None`` (unbounded):

    * per-day notional: the day's already-counted notional for ``account`` plus THIS
      intent's notional must not exceed ``max_daily_notional``.
    * max-concurrent: the count of OPEN positions for ``account`` must be below
      ``max_concurrent`` (blocks at/over the cap).
    * per-day realized loss (an R circuit breaker -- see the module docstring): the
      account's summed realized R for trades that exited today must not be at/below
      ``-max_daily_loss``.
    """
    if limits.max_daily_notional is not None:
        booked = sum(e.notional for e in execution_logs_for_day(
            session, run_date=run_date, account=account))
        if booked + notional(intent) > limits.max_daily_notional:
            return (
                f"per-day notional cap: {booked + notional(intent):.2f} "
                f"> {limits.max_daily_notional:.2f}"
            )
    if limits.max_concurrent is not None:
        open_count = count_open_positions(session, account=account)
        if open_count >= limits.max_concurrent:
            return f"max-concurrent positions cap: {open_count} >= {limits.max_concurrent}"
    if limits.max_daily_loss is not None:
        day_r = realized_r_on(session, run_date=run_date, account=account)
        if day_r <= -limits.max_daily_loss:
            return (
                f"per-day realized-loss breaker: {day_r:.2f}R "
                f"<= -{limits.max_daily_loss:.2f}R"
            )
    return None


class NoOpAdapter:
    """The default ("off") adapter: writes NOTHING, opens nothing, returns skipped.

    Exactly today's behavior -- the screener surfaces intents but never acts. The
    ``limits`` arg is accepted for protocol conformance but never consulted (there is
    nothing to limit)."""

    name = "off"

    def submit(
        self, intent: OrderIntent, *, session: Session, run_date: date, limits: Limits
    ) -> OrderResult:
        return OrderResult(status="skipped", account=OFF_ACCOUNT, detail="execution off")


class ManualAdapter:
    """The ("manual") adapter: RECORDS an order ticket for the human to place by hand.

    Money never moves: no paper position opens, no broker is called -- a recorded row in
    ``execution_logs`` is the whole effect. ``submit`` enforces the hard limits FIRST
    (in code, from the passed ``limits``), clamping a breach to a logged ``skipped`` row,
    and is idempotent per ``(pick, run, side)`` via the unique ``idempotency_key``."""

    name = "manual"

    def submit(
        self, intent: OrderIntent, *, session: Session, run_date: date, limits: Limits
    ) -> OrderResult:
        key = idempotency_key(intent, run_date)
        reason = _limit_block(
            session, intent, run_date=run_date, account=MANUAL_ACCOUNT, limits=limits)
        if reason is not None:
            # CLAMP: log the breach for audit; record/open NOTHING. The skipped status is
            # excluded from the limit-counting sums, so it never feeds back into the caps.
            add_execution_log(
                session, created_date=run_date, ticker=intent.ticker,
                timeframe=intent.timeframe, play_type=intent.play_type, run_date=run_date,
                account=MANUAL_ACCOUNT, mode="manual", side=intent.side,
                limit_price=intent.limit_price, shares=intent.shares, stop=intent.stop,
                target=intent.target, risk_dollars=intent.risk_dollars,
                notional=notional(intent), status="skipped", detail=reason,
                idempotency_key=key,
            )
            return OrderResult(status="skipped", account=MANUAL_ACCOUNT, detail=reason)
        ticket = (
            f"{intent.side} {intent.shares} {intent.ticker} @<= {intent.limit_price:.2f} "
            f"stop {intent.stop:.2f} target {intent.target:.2f}"
        )
        add_execution_log(
            session, created_date=run_date, ticker=intent.ticker,
            timeframe=intent.timeframe, play_type=intent.play_type, run_date=run_date,
            account=MANUAL_ACCOUNT, mode="manual", side=intent.side,
            limit_price=intent.limit_price, shares=intent.shares, stop=intent.stop,
            target=intent.target, risk_dollars=intent.risk_dollars,
            notional=notional(intent), status="recorded", detail=ticket,
            idempotency_key=key,
        )
        return OrderResult(
            status="recorded", account=MANUAL_ACCOUNT, detail="order ticket recorded",
            trade_id=None,
        )

"""The execution adapters: the seam between an ``OrderIntent`` and the outside world.

Phase 3 turns the insight engine's intents into ACTION through a small set of
pluggable adapters behind a single :class:`ExecutionAdapter` protocol:

* :class:`NoOpAdapter` (``name="off"``) -- the DEFAULT. Writes nothing, opens
  nothing, returns ``skipped``. Exactly today's behavior; the screener never arms
  by accident.
* :class:`ManualAdapter` (``name="manual"``) -- RECORDS an order ticket to the
  ``execution_logs`` audit table for the human to place by hand. Money never moves
  here: no paper position is opened, no broker is called.
* :class:`PaperAdapter` (``name="paper"``) -- OPENS one FILLED ``PaperTrade`` tagged
  ``account="paper"`` from the intent's fixed levels. No new lifecycle code: the
  EXISTING shadow stepper (``advance_open`` / ``evaluate_exit``) then fills, trails, and
  closes it. The ``account="paper"`` tag keeps the curated intent book out of every
  research aggregate; still real-money-free (no broker is called).
* :class:`LiveAdapter` (``name="live"``) -- Phase 4: submits ONE order to a real broker
  (through the injected ``BrokerClient``) and records the ``broker_order_id``, opening NO
  position (the fill price is unknown at submit; the reconciler materializes it later). A
  real-money endpoint arms ONLY behind all three locks AND every cap; a paper broker
  bypasses that guard. See the class for the full safety contract.

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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from sqlalchemy.orm import Session

from sqlalchemy import select

from swing_screener.db.models import ExecutionLog, PaperTrade
from swing_screener.db.repo import (
    add_execution_log,
    count_open_positions,
    execution_logs_for_day,
    realized_r_on,
    save_paper_trades,
)
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.broker import BrokerClient, BrokerOrderSpec
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.pipeline.variants import DEFAULT_VARIANT
from swing_screener.settings import (
    Limits,
    Settings,
    can_arm_real_money,
    load_settings,
    real_money_limits_ok,
)

# the account the manual adapter books tickets under -- NOT "paper" / "research".
MANUAL_ACCOUNT = "manual"
# the curated intent book: the paper adapter opens simulated fills here. Deliberately
# NOT "research" (the auto-booked grid) -- the account tag fences these out of every
# research aggregate (leaderboards + analyst calibration) while still letting the shared
# stepper advance them (load_open_paper_trades is account-inclusive).
PAPER_ACCOUNT = "paper"
# the NoOp adapter touches no book; it reports the research account purely as a label.
OFF_ACCOUNT = "research"
# the live adapter books order rows under "live" -- its own account so the (broker-placed)
# live orders keep their limit sums cleanly separate from manual / paper / research, and the
# reconciler (Task 5) materializes the eventual position under the same tag.
LIVE_ACCOUNT = "live"


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


class PaperAdapter:
    """The ("paper") adapter: OPENS a simulated position from an ``OrderIntent``.

    ``submit`` opens ONE filled ``PaperTrade`` (``account="paper"``) at the intent's
    fixed levels, then returns -- the EXISTING shadow stepper (``advance_open`` /
    ``evaluate_exit``) does all the fill/trail/close lifecycle, so no new lifecycle code
    lives here. The opened row mirrors the shadow book's freshly-filled open trade
    (concrete ``entry_price`` + strictly-positive ``risk``, ``high_water=entry_price``,
    ``hold_bars=0``, ``remaining_frac=1.0``, ``partial_done=False``) so the stepper's
    invariants (it asserts a concrete entry + risk) hold and its realized-R division
    never hits a zero divisor.

    Safety, all INSIDE ``submit`` (never trusting the caller): the hard limits are
    re-checked first (a breach CLAMPS to a logged ``skipped`` row, opening nothing); a
    non-positive risk (``limit_price <= stop``) is ``rejected`` (logged, opens nothing)
    rather than booked as a degenerate fill; and it is idempotent per ``(pick, run,
    side)`` -- a duplicate submit hands back the prior fill WITHOUT opening a second
    position (guarded on the existing ``filled_paper`` log for the key, belt-and-braces
    with the ExecutionLog unique constraint)."""

    name = "paper"

    def submit(
        self, intent: OrderIntent, *, session: Session, run_date: date, limits: Limits
    ) -> OrderResult:
        key = idempotency_key(intent, run_date)

        # No-double-open guard: if this intent x run already opened a paper position
        # (a `filled_paper` log row for `key`), hand the prior fill back rather than
        # opening a second one. The ExecutionLog unique key alone would dedupe the LOG,
        # but the PaperTrade has no such constraint -- so we must short-circuit here.
        prior = session.scalars(
            select(ExecutionLog).where(
                ExecutionLog.idempotency_key == key,
                ExecutionLog.status == "filled_paper",
            )
        ).first()
        if prior is not None:
            existing_open = session.scalars(
                select(PaperTrade).where(
                    PaperTrade.account == PAPER_ACCOUNT,
                    PaperTrade.ticker == intent.ticker,
                    PaperTrade.timeframe == intent.timeframe,
                    PaperTrade.play_type == intent.play_type,
                    PaperTrade.opened_date == run_date,
                )
            ).first()
            return OrderResult(
                status="filled_paper", account=PAPER_ACCOUNT,
                detail="paper position already open",
                trade_id=existing_open.id if existing_open is not None else None,
            )

        reason = _limit_block(
            session, intent, run_date=run_date, account=PAPER_ACCOUNT, limits=limits)
        if reason is not None:
            # CLAMP: log the breach for audit; open NOTHING. The skipped status is
            # excluded from the limit-counting sums, so it never feeds back into the caps.
            self._log(session, intent, run_date=run_date, key=key,
                      status="skipped", detail=reason)
            return OrderResult(status="skipped", account=PAPER_ACCOUNT, detail=reason)

        risk = intent.limit_price - intent.stop
        if risk <= 0:
            # A non-positive risk can't be honestly traded (the stepper would divide by
            # it for realized R) -> reject; log for audit, open nothing.
            detail = "non-positive risk"
            self._log(session, intent, run_date=run_date, key=key,
                      status="rejected", detail=detail)
            return OrderResult(status="rejected", account=PAPER_ACCOUNT, detail=detail)

        # Open ONE filled paper trade, mirroring the shadow book's freshly-filled open
        # row (shadow.open_from_signals): concrete entry + positive risk, status="open",
        # hold_bars=0; the runner-state defaults (high_water, remaining_frac=1.0,
        # partial_done=False) are seeded so the stepper reads concrete state from bar 1.
        pt = PaperTrade(
            account=PAPER_ACCOUNT,
            arm=BASELINE,
            variant=DEFAULT_VARIANT,
            ticker=intent.ticker,
            timeframe=intent.timeframe,
            horizon="",
            play_type=intent.play_type,
            signal_id=None,  # intents aren't 1:1 with a persisted signal (Phase-2 convention)
            signal_score=0.0,
            rank=0,
            fill_status="filled",
            status="open",
            entry_price=intent.limit_price,
            entry_date=run_date,
            opened_date=run_date,
            stop=intent.stop,
            target=intent.target,
            risk=risk,
            hold_bars=0,
            remaining_frac=1.0,
            partial_done=False,
            high_water=intent.limit_price,
        )
        save_paper_trades(session, [pt])  # add + commit -> pt.id is populated

        self._log(session, intent, run_date=run_date, key=key,
                  status="filled_paper", detail="paper position opened")
        return OrderResult(
            status="filled_paper", account=PAPER_ACCOUNT,
            detail="paper position opened", trade_id=pt.id,
        )

    @staticmethod
    def _log(
        session: Session, intent: OrderIntent, *, run_date: date, key: str,
        status: str, detail: str,
    ) -> None:
        """Append one paper ExecutionLog row (idempotent via the unique ``key``)."""
        add_execution_log(
            session, created_date=run_date, ticker=intent.ticker,
            timeframe=intent.timeframe, play_type=intent.play_type, run_date=run_date,
            account=PAPER_ACCOUNT, mode="paper", side=intent.side,
            limit_price=intent.limit_price, shares=intent.shares, stop=intent.stop,
            target=intent.target, risk_dollars=intent.risk_dollars,
            notional=notional(intent), status=status, detail=detail,
            idempotency_key=key,
        )


def _default_gate_ready(session: Session) -> bool:
    """Whether the advisory autonomy gate is ready (the live adapter's default seam).

    Imported locally so ``execution`` -> ``autonomy`` stays a runtime edge, not an import-time
    cycle (autonomy pulls in heavier analytics/repo modules). Tests inject ``gate_ready_fn``
    instead, so this is consulted only against a real DB-backed book in production."""
    from swing_screener.pipeline.autonomy import autonomy_gate

    return autonomy_gate(session).ready


class LiveAdapter:
    """The ("live") adapter: submits ONE order to a real broker, records the broker order id.

    The only adapter that talks to a venue. Unlike the paper adapter it opens NO position:
    at submit time the fill price is unknown, so a ``submitted_live`` ExecutionLog carrying
    the ``broker_order_id`` is the whole effect -- the reconciler (Task 5) later materializes
    the position from the broker's eventual fill. The order goes out as a ``day`` limit order
    keyed by the idempotency key as its ``client_order_id``, so a re-submit collapses to the
    same broker order (the broker is idempotent on it) and the same single log row.

    The load-bearing safety, all INSIDE ``submit`` (never trusting the caller):

    1. The REAL-MONEY guard, consulted ONLY when ``broker.is_real_money()`` -- a paper broker
       (Alpaca paper) needs no locks and skips it entirely. For a real-money endpoint it
       demands all THREE arming locks (``can_arm_real_money``: mode=live AND allow_real_money
       AND a ready gate) AND every hard cap set (``real_money_limits_ok``); either failing
       logs a ``rejected_live`` row and refuses, placing no broker order.
    2. The hard-limit clamp (``_limit_block``): a breach logs a ``skipped`` row and refuses
       BEFORE any broker call -- the venue is never touched on a clamped order.
    3. A graceful broker boundary: an exception from ``submit_order`` is caught and logged as
       ``rejected_live`` (never propagated); a broker-returned ``rejected`` order likewise.

    The settings + gate-readiness seams are injected so tests drive the real-money guard
    without a real gate or DB: ``settings`` defaults to ``load_settings()`` at submit, and
    ``gate_ready_fn`` defaults to the autonomy gate over the live session."""

    name = "live"

    def __init__(
        self,
        broker: BrokerClient,
        *,
        settings: Settings | None = None,
        gate_ready_fn: Callable[[Session], bool] | None = None,
    ) -> None:
        self._broker = broker
        # for can_arm_real_money; resolved lazily at submit (load_settings()) when not injected
        # so the live env is read fresh, not captured at construction.
        self._settings = settings
        # session -> bool; the real-money gate-readiness seam (default: the autonomy gate).
        self._gate_ready_fn = gate_ready_fn

    def submit(
        self, intent: OrderIntent, *, session: Session, run_date: date, limits: Limits
    ) -> OrderResult:
        key = idempotency_key(intent, run_date)

        # 1. REAL-MONEY guard -- consulted ONLY for a real-money endpoint. A paper broker
        #    (is_real_money() False) needs no locks and skips this block entirely.
        if self._broker.is_real_money():
            settings = self._settings or load_settings()
            gate_ready = (self._gate_ready_fn or _default_gate_ready)(session)
            ok, reason = can_arm_real_money(settings, gate_ready=gate_ready)
            if not ok:
                self._log(session, intent, run_date=run_date, key=key,
                          status="rejected_live", detail=reason)
                return OrderResult(status="rejected", account=LIVE_ACCOUNT, detail=reason)
            ok2, reason2 = real_money_limits_ok(limits)
            if not ok2:
                self._log(session, intent, run_date=run_date, key=key,
                          status="rejected_live", detail=reason2)
                return OrderResult(status="rejected", account=LIVE_ACCOUNT, detail=reason2)

        # 2. The hard-limit clamp -- BEFORE any broker call, so a clamped order never reaches
        #    the venue. A breach logs a skipped row (audit) and refuses.
        limit_reason = _limit_block(
            session, intent, run_date=run_date, account=LIVE_ACCOUNT, limits=limits)
        if limit_reason is not None:
            self._log(session, intent, run_date=run_date, key=key,
                      status="skipped", detail=limit_reason)
            return OrderResult(status="skipped", account=LIVE_ACCOUNT, detail=limit_reason)

        # 3. Submit to the venue, GRACEFULLY: any broker exception is logged + refused, never
        #    propagated. The idempotency key is the broker's client_order_id, so a re-submit
        #    collapses to the same broker order. BRACKET by default: the venue holds the
        #    protective stop + target itself, so a filled position stays protected even if
        #    the screener dies (SWING_BRACKET_ORDERS=off restores the plain limit entry
        #    with reconcile-managed exits). Levels are the intent's deterministic stop and
        #    target -- never computed here (North Star #4).
        bracket = (self._settings or load_settings()).bracket_orders
        try:
            order = self._broker.submit_order(BrokerOrderSpec(
                client_order_id=key, symbol=intent.ticker, side="buy", qty=intent.shares,
                order_type="limit", limit_price=intent.limit_price, time_in_force="day",
                stop_loss=intent.stop if bracket else None,
                take_profit=intent.target if bracket else None,
            ))
        except Exception as e:  # noqa: BLE001 -- a venue boundary: any failure must not raise.
            detail = f"broker error: {e}"
            self._log(session, intent, run_date=run_date, key=key,
                      status="rejected_live", detail=detail)
            return OrderResult(status="rejected", account=LIVE_ACCOUNT, detail=detail)

        if order.status == "rejected":
            self._log(session, intent, run_date=run_date, key=key,
                      status="rejected_live", detail="broker rejected",
                      broker_order_id=order.broker_order_id, broker_status=order.status)
            return OrderResult(
                status="rejected", account=LIVE_ACCOUNT, detail="broker rejected",
                broker_order_id=order.broker_order_id,
            )

        # The order is working at the venue. Record it -- broker_order_id + broker_status --
        # but open NO position: the fill price is unknown until the reconciler reads a fill.
        self._log(session, intent, run_date=run_date, key=key, status="submitted_live",
                  detail="order submitted", broker_order_id=order.broker_order_id,
                  broker_status=order.status)
        return OrderResult(
            status="submitted_live", account=LIVE_ACCOUNT, detail="order submitted",
            broker_order_id=order.broker_order_id,
        )

    def _log(
        self, session: Session, intent: OrderIntent, *, run_date: date, key: str,
        status: str, detail: str, broker_order_id: str | None = None,
        broker_status: str | None = None,
    ) -> None:
        """Append one live ExecutionLog row (idempotent via the unique ``key``).

        Carries the broker provenance (``broker`` name + the optional ``broker_order_id`` /
        ``broker_status``) so the audit row is the single source of truth for a live order
        the reconciler later picks up. The broker id/status stay None on the pre-broker exits
        (the real-money refusal, the limit skip, a submit exception)."""
        add_execution_log(
            session, created_date=run_date, ticker=intent.ticker,
            timeframe=intent.timeframe, play_type=intent.play_type, run_date=run_date,
            account=LIVE_ACCOUNT, mode="live", broker=self._broker.name,
            broker_order_id=broker_order_id, broker_status=broker_status, side=intent.side,
            limit_price=intent.limit_price, shares=intent.shares, stop=intent.stop,
            target=intent.target, risk_dollars=intent.risk_dollars,
            notional=notional(intent), status=status, detail=detail,
            idempotency_key=key,
        )

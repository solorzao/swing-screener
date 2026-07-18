"""Thin CRUD layer over the SQLAlchemy models for signals and trades."""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swing_screener.db.models import (
    AnalysisRequest,
    CoachDraftRequest,
    AnalystCall,
    EmailLog,
    ExecutionLog,
    ExitEvent,
    PaperTrade,
    ReversalFunnel,
    Signal,
    Trade,
    Universe,
)
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.variants import DEFAULT_VARIANT

if TYPE_CHECKING:
    from swing_screener.data.universe import UniverseEntry

log = logging.getLogger(__name__)


def save_signals(session: Session, signals: Sequence[Signal]) -> None:
    session.add_all(list(signals))
    session.commit()


def latest_signals(session: Session, run_date: date) -> list[Signal]:
    stmt = select(Signal).where(Signal.run_date == run_date).order_by(Signal.rank)
    return list(session.scalars(stmt))


def latest_run_date(session: Session) -> date | None:
    """The most recent run_date present in the signals table, or None if empty."""
    stmt = select(Signal.run_date).order_by(Signal.run_date.desc()).limit(1)
    return session.scalars(stmt).first()


def prior_first_seen(
    session: Session, before_date: date, *, lookback_runs: int = 5
) -> dict[tuple[str, str, str], date]:
    """Map ``(ticker, timeframe, play_type) -> first_seen_date`` carried forward from each
    setup's most recent prior appearance within the last ``lookback_runs`` runs.

    Used to carry a setup's streak-start forward so the repeat cooldown can age it out:
    today's signal inherits the ``first_seen_date`` of its most recent prior appearance
    among the last ``lookback_runs`` distinct run dates strictly before ``before_date``.
    Looking back over several runs -- not just the immediately-prior one -- means a setup
    that FLICKERS (fires, skips a run, fires again) keeps its streak instead of resetting to
    a fresh ``first_seen``; a fresh reset would defeat the cooldown and let the same play
    resurface indefinitely. A setup absent for the whole window starts a fresh streak.
    Querying ``run_date < before_date`` keeps a same-day re-run (delete + reinsert of today)
    from disturbing the result. A prior row whose ``first_seen_date`` is NULL (legacy) falls
    back to its own ``run_date``.
    """
    prev_dates = list(session.scalars(
        select(Signal.run_date)
        .where(Signal.run_date < before_date)
        .distinct()
        .order_by(Signal.run_date.desc())
        .limit(lookback_runs)
    ))
    if not prev_dates:
        return {}
    out: dict[tuple[str, str, str], date] = {}
    # Newest run first: the first row seen for a key is its most recent prior appearance.
    rows = session.scalars(
        select(Signal)
        .where(Signal.run_date.in_(prev_dates))
        .order_by(Signal.run_date.desc())
    )
    for r in rows:
        key = (r.ticker, r.timeframe, r.play_type)
        if key not in out:
            out[key] = r.first_seen_date or r.run_date
    return out


def delete_signals_for(session: Session, run_date: date) -> None:
    session.execute(delete(Signal).where(Signal.run_date == run_date))
    session.commit()


def delete_paper_trades_opened_on(session: Session, opened_date: date) -> None:
    session.execute(delete(PaperTrade).where(PaperTrade.opened_date == opened_date))
    session.commit()


def save_paper_trades(session: Session, trades: Sequence[PaperTrade]) -> None:
    session.add_all(list(trades))
    session.commit()


def booked_trigger_keys(
    session: Session,
    *,
    variant: str,
    keys: Sequence[tuple[str, str, str, datetime]],
) -> set[tuple[str, str, str, datetime]]:
    """The subset of ``(ticker, timeframe, play_type, trigger_ts)`` keys already booked
    for ``variant`` -- the cross-run shadow-booking dedup lookup (a weekly trigger is
    re-detected on every daily run of its week). One batched SELECT filtered by the
    run's tickers + trigger timestamps, intersected in Python (portable across SQLite
    and Azure SQL, no tuple-IN needed). Empty input -> empty set.
    """
    if not keys:
        return set()
    stmt = (
        select(PaperTrade.ticker, PaperTrade.timeframe, PaperTrade.play_type,
               PaperTrade.trigger_ts)
        .where(
            PaperTrade.variant == variant,
            PaperTrade.trigger_ts.is_not(None),
            PaperTrade.ticker.in_({k[0] for k in keys}),
            PaperTrade.trigger_ts.in_({k[3] for k in keys}),
        )
        .distinct()
    )
    existing = {(t, tf, pt, ts) for t, tf, pt, ts in session.execute(stmt)}
    return existing & set(keys)


def load_open_paper_trades(
    session: Session, *, exclude_live: bool = False
) -> list[PaperTrade]:
    """Every OPEN paper trade, ACROSS ALL accounts by default. NOT filtered by ``account``
    in the inclusive form -- ``advance_open`` must keep stepping the curated intent book's
    open trades alongside the research grid, and the reconciler reads it inclusively too;
    only the CLOSED-trade aggregates are pinned to ``account == "research"``.

    ``exclude_live=True`` is the BAR-STEPPER's loader: it drops ``account == "live"`` rows so
    the simulator never advances a broker-owned position. A live fill is filled+closed by the
    BROKER and materialized/reconciled by ``reconcile_live`` (the two engines are disjoint);
    if the stepper stepped a live row it would invent simulated fills over broker reality.
    ``advance_open`` passes ``exclude_live=True``; ``research`` + ``paper`` still step.
    ``!= "live"`` renders ``account <> 'live'`` (portable to SQL Server), not a boolean ``.is_()``."""
    stmt = select(PaperTrade).where(PaperTrade.status == "open")
    if exclude_live:
        stmt = stmt.where(PaperTrade.account != "live")
    return list(session.scalars(stmt))


def load_pending_paper_trades(session: Session) -> list[PaperTrade]:
    """Every PENDING resting-limit order (a reversal fill window still working).

    Pending rows are created only by the shadow booking path (``account="research"`` and
    the screen-variant books), so no account filter is needed; ``resolve_pending`` fills,
    invalidates, or expires each one.
    """
    return list(session.scalars(select(PaperTrade).where(PaperTrade.status == "pending")))


def load_open_live_trades(session: Session) -> list[PaperTrade]:
    """Every OPEN ``account == "live"`` paper trade -- the reconciler's own loader.

    The complement of the stepper's ``exclude_live`` view: ``reconcile_live`` reads exactly the
    broker-owned open positions to check for a venue-side close. Only ``status == "open"`` rows
    come back, so a row already ``closed`` by a prior reconcile is never re-closed (the exit
    reconciliation is idempotent on this filter). ``==`` renders ``col = 'x'`` (portable to SQL
    Server), not a boolean ``.is_()``."""
    stmt = select(PaperTrade).where(
        PaperTrade.status == "open", PaperTrade.account == "live"
    )
    return list(session.scalars(stmt))


def load_research_paper_trades(session: Session) -> list[PaperTrade]:
    """Every paper trade in the RESEARCH grid (``account == "research"``), open or closed.

    The performance/leaderboard loader: ``summarize`` / ``breakdown`` filter to
    closed-filled internally, so this only needs to fence off the curated intent book
    (``account == "paper"``) from the research leaderboards. ``== "research"`` renders
    ``account = 'x'`` (portable to SQL Server), not a boolean ``.is_()``."""
    stmt = select(PaperTrade).where(PaperTrade.account == "research")
    return list(session.scalars(stmt))


def load_open_forward_book(session: Session) -> list[PaperTrade]:
    """Every OPEN trade in the FORWARD book: the research grid pinned to
    (``arm == BASELINE``, ``variant == DEFAULT_VARIANT``).

    The running complement of ``load_closed_paper_trades``'s reflection slice --
    the same account/arm/variant pin, minus the closed/filled/realized filters --
    so the cockpit's open-book panel shows exactly the trades whose closes the
    reflection will later grade. Pinning arm+variant is what DEDUPES the shadow
    grid's arm x variant fill multiplication: each distinct entry appears ONCE,
    not once per exit arm and once per screen variant. ``==`` renders
    ``col = 'x'`` (portable to SQL Server), not a boolean ``.is_()``."""
    stmt = select(PaperTrade).where(
        PaperTrade.status == "open", PaperTrade.account == "research",
        PaperTrade.arm == BASELINE, PaperTrade.variant == DEFAULT_VARIANT,
    )
    return list(session.scalars(stmt))


def load_closed_paper_trades(
    session: Session, *, play_type: str | None = None, arm: str | None = None,
    variant: str | None = None,
) -> list[PaperTrade]:
    """Filled trades that have closed with a realized result, optionally faceted by
    play_type / arm / variant. The reflection grades the LIVE forward book at
    (arm=BASELINE, variant=DEFAULT_VARIANT) per play type.

    Pinned to the research grid (``account == "research"``) so a future curated intent
    book (paper-executed OrderIntents under ``account == "paper"``) never inflates the
    leaderboard or the analyst calibration. ``== "research"`` renders ``account = 'x'``
    (portable to SQL Server), not a boolean ``.is_()``."""
    stmt = select(PaperTrade).where(
        PaperTrade.status == "closed", PaperTrade.fill_status == "filled",
        PaperTrade.realized_r.is_not(None), PaperTrade.account == "research",
    )
    if play_type is not None:
        stmt = stmt.where(PaperTrade.play_type == play_type)
    if arm is not None:
        stmt = stmt.where(PaperTrade.arm == arm)
    if variant is not None:
        stmt = stmt.where(PaperTrade.variant == variant)
    return list(session.scalars(stmt))


def load_scored_analyst_calls(
    session: Session, *, play_type: str | None = None
) -> list[AnalystCall]:
    """The SCORED analyst calls (``scored_at`` set), optionally faceted by play type.

    The reflection's calibration note reads these to grade whether the analyst's
    conviction calls / nudges are proving out on the live shadow book."""
    stmt = select(AnalystCall).where(AnalystCall.scored_at.is_not(None))
    if play_type is not None:
        stmt = stmt.where(AnalystCall.play_type == play_type)
    return list(session.scalars(stmt))


# How long after a call its own pick can plausibly BOOK in the shadow book: the next run
# for 1d/4h (1-3 calendar days across a weekend), the period-end run + weekend for 1wk
# (~7-9 days). Anything beyond is a DIFFERENT setup that happens to share the pick keys --
# grading a call against it poisons the calibration record the autonomy gate reads.
# PUBLIC: the cockpit's freshness split (``analyst_call_freshness``) keys on the same
# window so the endpoint and the scorer agree by construction.
ANALYST_SCORE_WINDOW_DAYS = 10


def score_analyst_calls(session: Session) -> int:
    """Score each UNSCORED ``AnalystCall`` against its realized shadow-book outcome.

    The learning join (North Star #9): an analyst call on run ``d`` for a pick
    ``(ticker, timeframe, play_type)`` is graded by the BASELINE/DEFAULT paper trade
    that FILLED on the first run AFTER ``d`` for that same pick -- i.e. the closed
    ``PaperTrade`` (``arm == BASELINE``, ``variant == DEFAULT_VARIANT``, filled, with a
    realized R) whose ``opened_date`` is the EARLIEST strictly greater than the call's
    ``run_date`` AND within ``ANALYST_SCORE_WINDOW_DAYS`` of it. The shadow book
    paper-trades the PRIOR-bar signal (fired on ``d``, booked the next run; a 1wk pick
    books on its period-end run), so the bounded convention join on the pick keys is the
    robust link -- no Signal FK required. The bound is what keeps the join honest: a
    call whose own pick never booked/filled stays UNSCORED FOREVER (there is no outcome
    for what the analyst graded) instead of borrowing a later, unrelated trade's R.

    Stamps ``realized_r`` + ``scored_at`` (the trade's ``exit_date``, else today) onto
    each matched call. A pick that hasn't filled+closed yet stays unscored and is
    rescored on a later run. Already-scored calls are skipped (``scored_at`` set), so a
    re-run is idempotent. Returns the count newly scored; commits once.
    """
    unscored = list(session.scalars(
        select(AnalystCall).where(AnalystCall.scored_at.is_(None))
    ))
    scored = 0
    for call in unscored:
        # `==` for the string/enum facets (renders `col = 'x'`); `.is_not(None)` for the
        # NULL guard -- both portable to SQL Server, unlike a boolean `.is_(0)`.
        window_end = call.run_date + timedelta(days=ANALYST_SCORE_WINDOW_DAYS)
        trade = session.scalars(
            select(PaperTrade).where(
                PaperTrade.ticker == call.ticker,
                PaperTrade.timeframe == call.timeframe,
                PaperTrade.play_type == call.play_type,
                PaperTrade.account == "research",
                PaperTrade.arm == BASELINE,
                PaperTrade.variant == DEFAULT_VARIANT,
                PaperTrade.status == "closed",
                PaperTrade.fill_status == "filled",
                PaperTrade.realized_r.is_not(None),
                PaperTrade.opened_date > call.run_date,
                PaperTrade.opened_date <= window_end,
            ).order_by(PaperTrade.opened_date).limit(1)
        ).first()
        if trade is None:
            continue
        call.realized_r = trade.realized_r
        call.scored_at = trade.exit_date or date.today()
        scored += 1
    session.commit()
    return scored


def analyst_call_freshness(
    calls: Sequence[AnalystCall], today: date
) -> dict[str, int]:
    """Three-way freshness split of ``calls`` -- the "unfilled fraction" read.

    PURE, beside ``score_analyst_calls`` on purpose: both key on the SAME
    ``ANALYST_SCORE_WINDOW_DAYS``, so the cockpit's split and the scorer's join
    agree by construction (a window change moves both at once).

      * ``scored``: ``scored_at`` set -- the call's own pick booked, filled, and
        closed inside the window; its R is on the record.
      * ``pending_in_window``: unscored with ``today <= run_date + WINDOW`` -- a
        trade can still open inside the scorer's join bound, so the call may yet
        score.
      * ``expired_unfilled``: unscored with the window passed -- the scorer will
        never borrow a later trade for it, so it stays unscored forever. This is
        the fraction of the analyst's judgments the shadow book never tested.

    Stated approximation: a pick that FILLED inside the window but has not CLOSED
    yet also counts here (calls alone cannot see open trades) -- it moves to
    ``scored`` when the close lands, so the count self-corrects; it never
    undercounts the true expired set.
    """
    scored = pending = expired = 0
    for call in calls:
        if call.scored_at is not None:
            scored += 1
        elif today <= call.run_date + timedelta(days=ANALYST_SCORE_WINDOW_DAYS):
            pending += 1
        else:
            expired += 1
    return {"scored": scored, "pending_in_window": pending,
            "expired_unfilled": expired}


# the statuses that COUNT against the per-day hard limits: a row only loads against the
# notional/loss sums if the order actually submitted and reserved its notional. The paper
# statuses ``recorded`` / ``filled_paper`` plus the Phase 4 live statuses ``submitted_live``
# (working, not yet filled) / ``filled_live`` all reserve it. ``skipped`` / ``rejected`` and
# the live ``canceled`` / ``rejected_live`` never reserved notional, so they don't count.
_LIMIT_COUNTING_STATUSES = ("recorded", "filled_paper", "submitted_live", "filled_live")


def add_execution_log(session: Session, **fields: object) -> ExecutionLog:
    """Append one ExecutionLog row; the unique ``idempotency_key`` makes it idempotent.

    The Phase 3 idempotency guard: every adapter call carries an ``idempotency_key``
    unique per intent x run, so a force-resent or hourly-digest re-run that tries to log
    the SAME order hits the unique constraint. On that ``IntegrityError`` we roll back and
    return the EXISTING row for that key -- a no-op that hands back the first record rather
    than double-submitting. add -> commit -> refresh on the happy path.
    """
    row = ExecutionLog(**fields)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        key = fields["idempotency_key"]
        existing = session.scalars(
            select(ExecutionLog).where(ExecutionLog.idempotency_key == key)
        ).one()
        return existing
    session.refresh(row)
    return row


def execution_logs_for_day(
    session: Session, *, run_date: date, account: str
) -> list[ExecutionLog]:
    """ExecutionLog rows for ``run_date`` + ``account`` that COUNT against the hard limits.

    The source for the per-day notional / loss sums: only rows whose order actually
    submitted (``status`` in ``recorded`` / ``filled_paper`` / ``submitted_live`` /
    ``filled_live``) load against the limits; ``skipped`` / ``canceled`` / ``rejected_live`` /
    ``rejected`` are excluded. ``==`` / ``.in_(...)`` render portably to SQL Server (no
    boolean ``.is_()``)."""
    stmt = select(ExecutionLog).where(
        ExecutionLog.run_date == run_date,
        ExecutionLog.account == account,
        ExecutionLog.status.in_(_LIMIT_COUNTING_STATUSES),
    )
    return list(session.scalars(stmt))


def latest_recorded_stop(session: Session, ticker: str) -> float | None:
    """The newest LIVE ticket's recorded stop level for ``ticker``, or None.

    The disarm restore path re-arms a dead bracket stop leg at this level -- COPIED
    from the ticket that placed the position (North Star #4: deterministic levels are
    ground truth), never computed fresh. Only live rows count (``submitted_live`` /
    ``filled_live``): manual/paper tickets never created venue exposure, so their
    levels never belong on a live venue order. None -> the caller refuses to guess."""
    stmt = (
        select(ExecutionLog.stop)
        .where(
            ExecutionLog.ticker == ticker,
            ExecutionLog.side == "buy",
            ExecutionLog.status.in_(("submitted_live", "filled_live")),
        )
        .order_by(ExecutionLog.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def realized_r_on(session: Session, *, run_date: date, account: str) -> float:
    """Sum of ``realized_r`` over CLOSED ``account`` trades whose ``exit_date == run_date``.

    The day's realized R for one account -- the input to the execution adapter's
    per-day-loss circuit breaker (a PRE-trade gate on how much the book has already
    given back today). Only closed trades with a realized result count; an open or
    unfilled trade contributes nothing. ``func.coalesce(..., 0.0)`` makes an empty
    day return 0.0 rather than NULL, and ``== "closed"`` / ``== account`` render
    ``col = 'x'`` (portable to SQL Server), not a boolean ``.is_()``."""
    stmt = select(func.coalesce(func.sum(PaperTrade.realized_r), 0.0)).where(
        PaperTrade.status == "closed",
        PaperTrade.account == account,
        PaperTrade.exit_date == run_date,
        PaperTrade.realized_r.is_not(None),
    )
    return float(session.scalar(stmt) or 0.0)


def count_open_positions(session: Session, *, account: str) -> int:
    """Count of OPEN ``PaperTrade`` rows for ``account`` (the per-account position cap).

    ``== "open"`` / ``== account`` render ``col = 'x'`` (portable to SQL Server), not a
    boolean ``.is_()``."""
    stmt = select(func.count()).select_from(PaperTrade).where(
        PaperTrade.status == "open", PaperTrade.account == account
    )
    return session.scalar(stmt) or 0


def record_exit_event(session: Session, *, is_paper: bool, trade_id: int | None,
                      tier: str, reason: str, message: str, created_date: date,
                      account: str = "research") -> ExitEvent:
    event = ExitEvent(created_date=created_date, is_paper=is_paper, trade_id=trade_id,
                      tier=tier, reason=reason, message=message, account=account)
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def exit_events_for(session: Session, created_date: date, *, is_paper: bool) -> list[ExitEvent]:
    """ExitEvents recorded on a given day for the paper / real book.

    Used by the intraday exit checker to dedupe by (trade_id, reason, created_date)
    so an hourly re-run never piles up duplicate alerts.
    """
    # `==` (renders `is_paper = 0/1`) not `.is_()` (renders `IS 0`, a syntax
    # error on SQL Server though valid on SQLite).
    stmt = select(ExitEvent).where(
        ExitEvent.created_date == created_date, ExitEvent.is_paper == is_paper
    )
    return list(session.scalars(stmt))


def add_trade(session: Session, trade: Trade) -> Trade:
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def get_open_trades(session: Session) -> list[Trade]:
    return list(session.scalars(select(Trade).where(Trade.status == "open")))


def signal_play_types(session: Session, signal_ids: list[int]) -> dict[int, str]:
    """``{signal_id: play_type}`` for the given signal ids (one batch query).

    Lets a real ``Trade`` resolve its play type through the ``signal_id`` FK -- the
    exit policy differs per play type (the momentum-flip exit is a net drag on
    reversals), and Trade rows don't carry it themselves. Unknown/absent ids are
    simply missing from the result (callers fall back to "continuation")."""
    if not signal_ids:
        return {}
    rows = session.execute(
        select(Signal.id, Signal.play_type).where(Signal.id.in_(signal_ids))
    ).all()
    return {sid: pt for sid, pt in rows}


def get_closed_trades(session: Session) -> list[Trade]:
    stmt = select(Trade).where(Trade.status == "closed").order_by(Trade.exit_date.desc())
    return list(session.scalars(stmt))


class AlreadyClosedError(ValueError):
    """Raised by ``close_trade`` on a trade that is already closed.

    A ``ValueError`` SUBCLASS so any existing caller catching ``ValueError`` keeps
    working; the cockpit close endpoint tells the two flavors apart by type
    (unknown id -> 404, already closed -> 409) instead of string-matching messages.
    """


def _open_trade_or_raise(session: Session, trade_id: int) -> Trade:
    """Fetch a trade for closing: plain ``ValueError`` on an unknown id,
    ``AlreadyClosedError`` on a closed one. Refused HERE, once, rather than in
    every caller (the Streamlit form only OFFERED open trades; an HTTP endpoint
    can be raced or replayed) -- re-closing would silently overwrite the exit."""
    trade = session.get(Trade, trade_id)
    if trade is None:
        raise ValueError(f"no trade with id {trade_id}")
    if trade.status == "closed":
        raise AlreadyClosedError(f"trade {trade_id} is already closed")
    return trade


def _apply_close(trade: Trade, *, exit_date: date, exit_price: float,
                 exit_reason: str) -> None:
    trade.status = "closed"
    trade.exit_date = exit_date
    trade.exit_price = exit_price
    trade.exit_reason = exit_reason


def close_trade(session: Session, trade_id: int, *, exit_date: date, exit_price: float,
                exit_reason: str) -> Trade:
    trade = _open_trade_or_raise(session, trade_id)
    _apply_close(trade, exit_date=exit_date, exit_price=exit_price,
                 exit_reason=exit_reason)
    session.commit()
    session.refresh(trade)
    return trade


def close_trade_with_event(
    session: Session, trade_id: int, *, exit_date: date, exit_price: float,
    exit_reason: str, event_reason: str, event_message: str, created_date: date,
    tier: str = "", account: str = "research",
) -> tuple[Trade, ExitEvent]:
    """Close a trade AND record its ExitEvent in ONE transaction.

    The cockpit's manual close needs both rows or neither: with separate commits
    (``close_trade`` then ``record_exit_event``) a failure between them leaves the
    trade durably closed while the client sees a 503 -- the retry then 409s
    confusingly, and the audit ExitEvent is PERMANENTLY missing (the exit
    change-token watermark never moves for that close). One commit carries both
    rows, so the failure mode is all-or-nothing: either both land or the trade is
    still open and a retry succeeds cleanly. Raises exactly like ``close_trade``.

    ATOMIC, not check-then-act: the close is one ``UPDATE ... WHERE
    status='open'`` -- a ``_open_trade_or_raise`` fetch would re-read this
    session's identity map, so two concurrent closes (each having read the trade
    open) would BOTH pass the check and the loser would silently overwrite the
    recorded exit. Zero rows matched means someone else won (or the id is
    unknown): re-read the row to raise the same errors ``close_trade`` does --
    plain ``ValueError`` on an unknown id, ``AlreadyClosedError`` otherwise --
    and the first close's exit fields stand untouched.
    """
    matched = session.execute(
        update(Trade)
        .where(Trade.id == trade_id, Trade.status == "open")
        .values(status="closed", exit_date=exit_date, exit_price=exit_price,
                exit_reason=exit_reason)
    ).rowcount
    if matched == 0:
        session.rollback()  # end the no-op write txn; expire any stale identity map
        trade = session.get(Trade, trade_id)
        if trade is None:
            raise ValueError(f"no trade with id {trade_id}")
        raise AlreadyClosedError(f"trade {trade_id} is already closed")
    event = ExitEvent(created_date=created_date, is_paper=False, trade_id=trade_id,
                      tier=tier, reason=event_reason, message=event_message,
                      account=account)
    session.add(event)
    session.commit()
    trade = session.get(Trade, trade_id)
    if trade is None:  # unreachable: the UPDATE just matched this exact row
        raise ValueError(f"no trade with id {trade_id}")
    session.refresh(event)
    return trade, event


def update_trade(session: Session, trade_id: int, **fields: object) -> Trade:
    trade = session.get(Trade, trade_id)
    if trade is None:
        raise ValueError(f"no trade with id {trade_id}")
    for key, value in fields.items():
        setattr(trade, key, value)
    session.commit()
    session.refresh(trade)
    return trade


def list_universe(session: Session, search: str | None = None) -> list[Universe]:
    stmt = select(Universe).order_by(Universe.ticker)
    if search:
        term = search.upper().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Universe.ticker.like(f"%{term}%", escape="\\"))
    return list(session.scalars(stmt))


def sync_universe(session: Session, entries: "Sequence[UniverseEntry]") -> None:
    """Mirror the screening universe: upsert ticker->name/exchange and delete tickers no
    longer in the seed. Existing market_cap/avg_dollar_volume are preserved."""
    incoming = {e.ticker: e for e in entries}
    existing = {u.ticker: u for u in session.scalars(select(Universe))}
    for ticker, e in incoming.items():
        row = existing.get(ticker)
        if row is None:
            session.add(Universe(ticker=ticker, name=e.name, exchange=e.exchange))
        else:
            row.name = e.name
            row.exchange = e.exchange
    for ticker, row in existing.items():
        if ticker not in incoming:
            session.delete(row)
    session.commit()


def apply_universe_metrics(
    session: Session, metrics: "Mapping[str, Mapping[str, float | str | None]]"
) -> None:
    """Update market_cap / avg_dollar_volume / sector for known tickers; skip None values
    and unknown tickers (so a transient fetch failure preserves the prior value). Single
    commit."""
    if not metrics:
        return
    rows = {u.ticker: u for u in session.scalars(
        select(Universe).where(Universe.ticker.in_(list(metrics))))}
    for ticker, vals in metrics.items():
        row = rows.get(ticker)
        if row is None:
            continue
        mc = vals.get("market_cap")
        if mc is not None:
            row.market_cap = float(mc)
        adv = vals.get("avg_dollar_volume")
        if adv is not None:
            row.avg_dollar_volume = float(adv)
        sector = vals.get("sector")
        if sector is not None:
            row.sector = str(sector)
    session.commit()


def list_email_log(session: Session) -> list[EmailLog]:
    return list(session.scalars(select(EmailLog).order_by(EmailLog.sent_at.desc())))


def save_reversal_funnel(
    session: Session, *, run_date: date, detected: int, confirmed: int, fresh: int,
    actionable: int, surfaced: int, overflow_tickers: str, pool_n: int,
    confirmed_only: bool, premium_only: bool, already_ran_checked: bool,
) -> ReversalFunnel:
    """Persist the daily digest's reversal funnel snapshot, ONE row per ``run_date``.

    Delete-then-insert (the ``delete_signals_for`` rewrite posture): a forced digest
    resend re-executes the funnel write for the same run_date, and the latest
    execution's counts must win -- not raise on the unique index or pile up
    duplicates. The fresh/actionable/surfaced stages are digest-time state (cooldown,
    live quotes, sector cap) and are unrecoverable later, so this row is the only
    record. ``created_at`` is stamped in UTC at save time (mirrors MarketReport).

    CONCURRENT-REPLICA race (the Sunday double-fire that bit market_run in 2026-06):
    two same-run_date runs can both pass the delete and both insert; the loser hits
    the unique index HERE, after real work (the continuation Opus spend) is done.
    Losing is benign -- the winner's row was computed from the same DB state -- so
    the loser rolls back, logs, and returns the winner's row instead of dying."""
    session.execute(delete(ReversalFunnel).where(ReversalFunnel.run_date == run_date))
    row = ReversalFunnel(
        run_date=run_date, detected=detected, confirmed=confirmed, fresh=fresh,
        actionable=actionable, surfaced=surfaced, overflow_tickers=overflow_tickers,
        pool_n=pool_n, confirmed_only=confirmed_only, premium_only=premium_only,
        already_ran_checked=already_ran_checked, created_at=datetime.now(UTC),
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:  # lost the concurrent-replica race; keep the winner's row
        session.rollback()
        log.warning("reversal funnel for %s already recorded by a concurrent run; "
                    "keeping the existing row", run_date)
        return session.scalars(
            select(ReversalFunnel).where(ReversalFunnel.run_date == run_date)
        ).one()
    session.refresh(row)
    return row


def latest_reversal_funnel(session: Session) -> ReversalFunnel | None:
    """The newest funnel snapshot by run_date, or None when nothing has been recorded.

    The cockpit's funnel view reads this -- the latest daily digest's stage counts."""
    stmt = select(ReversalFunnel).order_by(ReversalFunnel.run_date.desc()).limit(1)
    return session.scalars(stmt).first()


def create_analysis_request(session: Session, *, ticker: str, requested_at: datetime,
                            recipient: str = "") -> AnalysisRequest:
    req = AnalysisRequest(ticker=ticker, requested_at=requested_at, recipient=recipient)
    session.add(req)
    session.commit()
    session.refresh(req)
    return req


def list_analysis_requests(session: Session, limit: int = 50) -> list[AnalysisRequest]:
    stmt = select(AnalysisRequest).order_by(AnalysisRequest.requested_at.desc()).limit(limit)
    return list(session.scalars(stmt))


def get_analysis_request(session: Session, request_id: int) -> AnalysisRequest | None:
    return session.get(AnalysisRequest, request_id)


def claim_queued_requests(session: Session, *, now: datetime,
                          limit: int = 10) -> list[AnalysisRequest]:
    """Atomically flip queued->running and return the claimed rows."""
    ids = list(session.scalars(
        select(AnalysisRequest.id).where(AnalysisRequest.status == "queued")
        .order_by(AnalysisRequest.requested_at).limit(limit)))
    if not ids:
        return []
    session.execute(update(AnalysisRequest)
        .where(AnalysisRequest.id.in_(ids), AnalysisRequest.status == "queued")
        .values(status="running", started_at=now))
    session.commit()
    # Self-identifying read-back: only return rows THIS call stamped with `now`.
    # Safe under concurrent replicas -- each stamps its own `now`, so the loser of a
    # race re-reads zero of the winner's rows instead of double-processing them.
    return list(session.scalars(
        select(AnalysisRequest).where(AnalysisRequest.id.in_(ids),
                                      AnalysisRequest.status == "running",
                                      AnalysisRequest.started_at == now)
        .order_by(AnalysisRequest.requested_at)))


def requeue_stale_running(session: Session, *, cutoff: datetime) -> int:
    """Reset rows stuck 'running' since before `cutoff` back to 'queued' so a crashed/
    retried worker re-processes them. Returns the count requeued."""
    result = session.execute(
        update(AnalysisRequest)
        .where(AnalysisRequest.status == "running", AnalysisRequest.started_at < cutoff)
        .values(status="queued", started_at=None))
    session.commit()
    # `Session.execute` is typed `Result`; an UPDATE actually yields a `CursorResult`,
    # which is what carries `rowcount`.
    return cast("CursorResult[Any]", result).rowcount


def complete_analysis_request(session: Session, request_id: int, *, summary: str,
                              pdf_blob_key: str | None, chart_blob_keys: str,
                              finished_at: datetime) -> None:
    req = session.get(AnalysisRequest, request_id)
    if req is None:
        return
    req.status = "done"
    req.summary = summary
    req.pdf_blob_key = pdf_blob_key
    req.chart_blob_keys = chart_blob_keys
    req.finished_at = finished_at
    session.commit()


def fail_analysis_request(session: Session, request_id: int, *, error: str,
                          finished_at: datetime) -> None:
    req = session.get(AnalysisRequest, request_id)
    if req is None:
        return
    req.status = "failed"
    req.error = error
    req.finished_at = finished_at
    session.commit()


def create_coach_draft_request(session: Session, *, review_id: int,
                               requested_at: datetime) -> CoachDraftRequest:
    req = CoachDraftRequest(review_id=review_id, requested_at=requested_at)
    session.add(req)
    session.commit()
    session.refresh(req)
    return req


def claim_queued_coach_drafts(session: Session, *, now: datetime,
                              limit: int = 10) -> list[CoachDraftRequest]:
    """Atomically flip queued->running and return the claimed rows (self-identifying
    read-back by ``started_at == now`` -- race-safe across concurrent workers)."""
    ids = list(session.scalars(
        select(CoachDraftRequest.id).where(CoachDraftRequest.status == "queued")
        .order_by(CoachDraftRequest.requested_at).limit(limit)))
    if not ids:
        return []
    session.execute(update(CoachDraftRequest)
        .where(CoachDraftRequest.id.in_(ids), CoachDraftRequest.status == "queued")
        .values(status="running", started_at=now))
    session.commit()
    return list(session.scalars(
        select(CoachDraftRequest).where(CoachDraftRequest.id.in_(ids),
                                        CoachDraftRequest.status == "running",
                                        CoachDraftRequest.started_at == now)
        .order_by(CoachDraftRequest.requested_at)))


def requeue_stale_coach_drafts(session: Session, *, cutoff: datetime) -> int:
    """Reset rows stuck 'running' since before `cutoff` back to 'queued' (crashed-worker
    recovery). Returns the count requeued."""
    result = session.execute(
        update(CoachDraftRequest)
        .where(CoachDraftRequest.status == "running", CoachDraftRequest.started_at < cutoff)
        .values(status="queued", started_at=None))
    session.commit()
    return cast("CursorResult[Any]", result).rowcount


def complete_coach_draft_request(session: Session, request_id: int, *,
                                 finished_at: datetime) -> None:
    req = session.get(CoachDraftRequest, request_id)
    if req is None:
        return
    req.status = "done"
    req.finished_at = finished_at
    session.commit()


def fail_coach_draft_request(session: Session, request_id: int, *, error: str,
                             finished_at: datetime) -> None:
    req = session.get(CoachDraftRequest, request_id)
    if req is None:
        return
    req.status = "failed"
    req.error = error
    req.finished_at = finished_at
    session.commit()

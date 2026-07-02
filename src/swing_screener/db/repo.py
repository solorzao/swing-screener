"""Thin CRUD layer over the SQLAlchemy models for signals and trades."""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swing_screener.db.models import (
    AnalysisRequest,
    AnalystCall,
    EmailLog,
    ExecutionLog,
    ExitEvent,
    PaperTrade,
    Signal,
    Trade,
    Universe,
)
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.variants import DEFAULT_VARIANT

if TYPE_CHECKING:
    from swing_screener.data.universe import UniverseEntry


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


def score_analyst_calls(session: Session) -> int:
    """Score each UNSCORED ``AnalystCall`` against its realized shadow-book outcome.

    The learning join (North Star #9): an analyst call on run ``d`` for a pick
    ``(ticker, timeframe, play_type)`` is graded by the BASELINE/DEFAULT paper trade
    that FILLED on the first run AFTER ``d`` for that same pick -- i.e. the closed
    ``PaperTrade`` (``arm == BASELINE``, ``variant == DEFAULT_VARIANT``, filled, with a
    realized R) whose ``opened_date`` is the EARLIEST strictly greater than the call's
    ``run_date``. The shadow book paper-trades the PRIOR-bar signal (fired on ``d``,
    filled on ``d+1``), so this convention join on the pick keys + earliest post-call
    fill is the robust link -- no Signal FK required.

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
            ).order_by(PaperTrade.opened_date).limit(1)
        ).first()
        if trade is None:
            continue
        call.realized_r = trade.realized_r
        call.scored_at = trade.exit_date or date.today()
        scored += 1
    session.commit()
    return scored


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


def get_closed_trades(session: Session) -> list[Trade]:
    stmt = select(Trade).where(Trade.status == "closed").order_by(Trade.exit_date.desc())
    return list(session.scalars(stmt))


def close_trade(session: Session, trade_id: int, *, exit_date: date, exit_price: float,
                exit_reason: str) -> Trade:
    trade = session.get(Trade, trade_id)
    if trade is None:
        raise ValueError(f"no trade with id {trade_id}")
    trade.status = "closed"
    trade.exit_date = exit_date
    trade.exit_price = exit_price
    trade.exit_reason = exit_reason
    session.commit()
    session.refresh(trade)
    return trade


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

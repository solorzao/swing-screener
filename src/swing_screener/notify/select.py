"""Digest selection queries over the SQLite store.

The digest orchestrator uses these to pick which signals appear in each
cadence (daily / weekly / monthly) and which exit alerts to send.
"""

from datetime import date, timedelta

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from swing_screener.db.models import ExitEvent, Signal


def _fresh_enough(run_date: date, max_age_days: int | None) -> list[ColumnElement[bool]]:
    """A staleness/cooldown WHERE clause (as a list to splat into ``.where``).

    When ``max_age_days`` is set, keep only setups first seen within the window --
    a pick whose ``first_seen_date`` is older than ``run_date - max_age_days`` has
    been on the list too long and is dropped, so the same play isn't re-pitched day
    after day. Legacy rows (NULL ``first_seen_date``) are never dropped (fail-open).
    ``None`` disables the cooldown entirely.
    """
    if max_age_days is None:
        return []
    cutoff = run_date - timedelta(days=max_age_days)
    return [or_(Signal.first_seen_date.is_(None), Signal.first_seen_date >= cutoff)]


def daily_picks(session: Session, run_date: date, *, top_n: int = 5,
                max_age_days: int | None = None) -> list[Signal]:
    """Top-N CONTINUATION signals overall for the run date (any timeframe).

    The daily digest is the day's best continuation picks across all timeframes;
    weekly_picks/monthly_picks are the timeframe-specific cadences, and
    reversal_picks is the separate oversold-bounce list. ``max_age_days`` applies the
    staleness cooldown (see ``_fresh_enough``).
    """
    stmt = (
        select(Signal)
        .where(Signal.run_date == run_date, Signal.play_type == "continuation",
               *_fresh_enough(run_date, max_age_days))
        .order_by(Signal.rank)
        .limit(top_n)
    )
    return list(session.scalars(stmt))


def reversal_picks(session: Session, run_date: date, *, top_n: int = 5,
                   max_age_days: int | None = None) -> list[Signal]:
    """Top-N REVERSAL signals overall for the run date (any timeframe) -- the
    oversold-bounce / relief-rally list, ranked best first."""
    stmt = (
        select(Signal)
        .where(Signal.run_date == run_date, Signal.play_type == "reversal",
               *_fresh_enough(run_date, max_age_days))
        .order_by(Signal.rank)
        .limit(top_n)
    )
    return list(session.scalars(stmt))


def _by_timeframe(session: Session, run_date: date, timeframe: str, top_n: int,
                  max_age_days: int | None) -> list[Signal]:
    stmt = (
        select(Signal)
        .where(Signal.run_date == run_date, Signal.timeframe == timeframe,
               Signal.play_type == "continuation",
               *_fresh_enough(run_date, max_age_days))
        .order_by(Signal.rank)
        .limit(top_n)
    )
    return list(session.scalars(stmt))


def weekly_picks(session: Session, run_date: date, *, top_n: int = 5,
                 max_age_days: int | None = None) -> list[Signal]:
    """Top-N weekly-timeframe signals for the run date."""
    return _by_timeframe(session, run_date, "1wk", top_n, max_age_days)


def monthly_picks(session: Session, run_date: date, *, top_n: int = 5,
                  max_age_days: int | None = None) -> list[Signal]:
    """Top-N monthly-timeframe signals for the run date."""
    return _by_timeframe(session, run_date, "1mo", top_n, max_age_days)


def pending_exit_alerts(session: Session, run_date: date) -> list[ExitEvent]:
    """Exit events for REAL trades on the given date (paper-trade events excluded)."""
    # `== False` renders `is_paper = 0`; `.is_(False)` renders `IS 0`, which is a
    # syntax error on SQL Server (valid only on SQLite).
    stmt = (
        select(ExitEvent)
        .where(ExitEvent.created_date == run_date, ExitEvent.is_paper == False)  # noqa: E712
        .order_by(ExitEvent.id.desc())
    )
    return list(session.scalars(stmt))

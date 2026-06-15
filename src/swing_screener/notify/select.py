"""Digest selection queries over the SQLite store.

The digest orchestrator uses these to pick which signals appear in each
cadence (daily / weekly / monthly) and which exit alerts to send.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import ExitEvent, Signal


def daily_picks(session: Session, run_date: date, *, top_n: int = 5) -> list[Signal]:
    """Top-N CONTINUATION signals overall for the run date (any timeframe).

    The daily digest is the day's best continuation picks across all timeframes;
    weekly_picks/monthly_picks are the timeframe-specific cadences, and
    reversal_picks is the separate oversold-bounce list.
    """
    stmt = (
        select(Signal)
        .where(Signal.run_date == run_date, Signal.play_type == "continuation")
        .order_by(Signal.rank)
        .limit(top_n)
    )
    return list(session.scalars(stmt))


def reversal_picks(session: Session, run_date: date, *, top_n: int = 5) -> list[Signal]:
    """Top-N REVERSAL signals overall for the run date (any timeframe) -- the
    oversold-bounce / relief-rally list, ranked best first."""
    stmt = (
        select(Signal)
        .where(Signal.run_date == run_date, Signal.play_type == "reversal")
        .order_by(Signal.rank)
        .limit(top_n)
    )
    return list(session.scalars(stmt))


def _by_timeframe(session: Session, run_date: date, timeframe: str, top_n: int) -> list[Signal]:
    stmt = (
        select(Signal)
        .where(Signal.run_date == run_date, Signal.timeframe == timeframe,
               Signal.play_type == "continuation")
        .order_by(Signal.rank)
        .limit(top_n)
    )
    return list(session.scalars(stmt))


def weekly_picks(session: Session, run_date: date, *, top_n: int = 5) -> list[Signal]:
    """Top-N weekly-timeframe signals for the run date."""
    return _by_timeframe(session, run_date, "1wk", top_n)


def monthly_picks(session: Session, run_date: date, *, top_n: int = 5) -> list[Signal]:
    """Top-N monthly-timeframe signals for the run date."""
    return _by_timeframe(session, run_date, "1mo", top_n)


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

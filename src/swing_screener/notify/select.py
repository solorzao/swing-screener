"""Digest selection queries over the SQLite store.

The digest orchestrator uses these to pick which signals appear in each
cadence (daily / weekly / monthly) and which exit alerts to send.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import ExitEvent, Signal


def daily_picks(session: Session, run_date: date, *, top_n: int = 5) -> list[Signal]:
    """Top-N daily-timeframe signals for the run date, ordered by rank (best first)."""
    return _by_timeframe(session, run_date, "1d", top_n)


def _by_timeframe(session: Session, run_date: date, timeframe: str, top_n: int) -> list[Signal]:
    stmt = (
        select(Signal)
        .where(Signal.run_date == run_date, Signal.timeframe == timeframe)
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
    stmt = (
        select(ExitEvent)
        .where(ExitEvent.created_date == run_date, ExitEvent.is_paper.is_(False))
        .order_by(ExitEvent.id.desc())
    )
    return list(session.scalars(stmt))

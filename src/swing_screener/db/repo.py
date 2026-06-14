"""Thin CRUD layer over the SQLAlchemy models for signals and trades."""

from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import ExitEvent, PaperTrade, Signal


def save_signals(session: Session, signals: Sequence[Signal]) -> None:
    session.add_all(list(signals))
    session.commit()


def latest_signals(session: Session, run_date: date) -> list[Signal]:
    stmt = select(Signal).where(Signal.run_date == run_date).order_by(Signal.rank)
    return list(session.scalars(stmt))


def save_paper_trades(session: Session, trades: Sequence[PaperTrade]) -> None:
    session.add_all(list(trades))
    session.commit()


def load_open_paper_trades(session: Session) -> list[PaperTrade]:
    stmt = select(PaperTrade).where(PaperTrade.status == "open")
    return list(session.scalars(stmt))


def record_exit_event(session: Session, *, is_paper: bool, trade_id: int | None,
                      tier: str, reason: str, message: str, created_date: date) -> ExitEvent:
    event = ExitEvent(created_date=created_date, is_paper=is_paper, trade_id=trade_id,
                      tier=tier, reason=reason, message=message)
    session.add(event)
    session.commit()
    session.refresh(event)
    return event

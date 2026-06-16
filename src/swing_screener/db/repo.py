"""Thin CRUD layer over the SQLAlchemy models for signals and trades."""

from collections.abc import Mapping, Sequence
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from swing_screener.db.models import EmailLog, ExitEvent, PaperTrade, Signal, Trade, Universe

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


def delete_signals_for(session: Session, run_date: date) -> None:
    session.execute(delete(Signal).where(Signal.run_date == run_date))
    session.commit()


def delete_paper_trades_opened_on(session: Session, opened_date: date) -> None:
    session.execute(delete(PaperTrade).where(PaperTrade.opened_date == opened_date))
    session.commit()


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
    session: Session, metrics: "Mapping[str, Mapping[str, float | None]]"
) -> None:
    """Update market_cap / avg_dollar_volume for known tickers; skip None values and
    unknown tickers. Single commit."""
    if not metrics:
        return
    rows = {u.ticker: u for u in session.scalars(
        select(Universe).where(Universe.ticker.in_(list(metrics))))}
    for ticker, vals in metrics.items():
        row = rows.get(ticker)
        if row is None:
            continue
        if vals.get("market_cap") is not None:
            row.market_cap = vals["market_cap"]
        if vals.get("avg_dollar_volume") is not None:
            row.avg_dollar_volume = vals["avg_dollar_volume"]
    session.commit()


def list_email_log(session: Session) -> list[EmailLog]:
    return list(session.scalars(select(EmailLog).order_by(EmailLog.sent_at.desc())))

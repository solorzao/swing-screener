"""SQLAlchemy 2.x typed declarative models for the screener's local store.

The same models are intended to later target Azure SQL, so they stick to
portable column types. Nullable fields are declared as ``Mapped[T | None]``
with ``default=None``.
"""

from datetime import date, datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all screener tables."""


class Universe(Base):
    __tablename__ = "universe"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    exchange: Mapped[str] = mapped_column(String(32), default="")
    market_cap: Mapped[float | None] = mapped_column(default=None)
    avg_dollar_volume: Mapped[float | None] = mapped_column(default=None)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_date: Mapped[date]
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(32))
    horizon: Mapped[str] = mapped_column(String(32))
    # "continuation" (the pullback engine) or "reversal" (the oversold-bounce engine).
    play_type: Mapped[str] = mapped_column(String(16), default="continuation", index=True)
    # reversal strength: "early" / "confirmed"; None for continuation plays.
    strength: Mapped[str | None] = mapped_column(String(16), default=None)
    score: Mapped[float]
    rank: Mapped[int]
    mtf_aligned: Mapped[bool] = mapped_column(default=False)
    quality_tier: Mapped[str] = mapped_column(String(32), default="")
    volatility_tier: Mapped[str] = mapped_column(String(32), default="")
    oversold: Mapped[bool] = mapped_column(default=False)
    trigger_close: Mapped[float]
    atr: Mapped[float]
    rsi: Mapped[float]
    entry_floor: Mapped[float]
    entry_ceiling: Mapped[float]
    stop: Mapped[float]
    target: Mapped[float]
    chart_path: Mapped[str | None] = mapped_column(String(512), default=None)


class Trade(Base):
    """Real / manually entered trades."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(32))
    horizon: Mapped[str] = mapped_column(String(32))
    entry_date: Mapped[date]
    entry_price: Mapped[float]
    size: Mapped[float]
    stop: Mapped[float]
    target: Mapped[float]
    status: Mapped[str] = mapped_column(String(32), default="open")
    exit_date: Mapped[date | None] = mapped_column(default=None)
    exit_price: Mapped[float | None] = mapped_column(default=None)
    exit_reason: Mapped[str | None] = mapped_column(String(32), default=None)
    notes: Mapped[str] = mapped_column(String(256), default="")
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), default=None)


class PaperTrade(Base):
    """Shadow book of simulated fills."""

    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(32))
    horizon: Mapped[str] = mapped_column(String(32))
    # denormalized from the signal so the shadow book can be sliced by play_type
    # (continuation vs reversal) in QC without a join.
    play_type: Mapped[str] = mapped_column(String(16), default="continuation", index=True)
    strength: Mapped[str | None] = mapped_column(String(16), default=None)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), default=None)
    signal_score: Mapped[float]
    rank: Mapped[int]
    mtf_aligned: Mapped[bool] = mapped_column(default=False)
    # categorization tags denormalized from the signal so the shadow book can be
    # sliced by them in QC without a join back to the (run-date-scoped) signal row.
    quality_tier: Mapped[str] = mapped_column(String(32), default="")
    volatility_tier: Mapped[str] = mapped_column(String(32), default="")
    oversold: Mapped[bool] = mapped_column(default=False)
    fill_status: Mapped[str] = mapped_column(String(32))  # filled / missed / invalidated
    entry_date: Mapped[date | None] = mapped_column(default=None)
    entry_price: Mapped[float | None] = mapped_column(default=None)
    opened_date: Mapped[date | None] = mapped_column(default=None)
    last_advanced: Mapped[date | None] = mapped_column(default=None)
    stop: Mapped[float]
    target: Mapped[float]
    risk: Mapped[float]
    status: Mapped[str] = mapped_column(String(32), default="open")
    exit_date: Mapped[date | None] = mapped_column(default=None)
    exit_price: Mapped[float | None] = mapped_column(default=None)
    exit_reason: Mapped[str | None] = mapped_column(String(32), default=None)
    realized_r: Mapped[float | None] = mapped_column(default=None)
    hold_bars: Mapped[int | None] = mapped_column(default=None)


class ExitEvent(Base):
    __tablename__ = "exit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_date: Mapped[date]
    is_paper: Mapped[bool] = mapped_column(default=False)
    trade_id: Mapped[int | None] = mapped_column(default=None)
    tier: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(String(256), default="")


class EmailLog(Base):
    __tablename__ = "email_log"
    __table_args__ = (
        UniqueConstraint("kind", "run_date", "alert_key", name="uq_email_log_dedup"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sent_at: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(32))  # daily / weekly / monthly / exit
    subject: Mapped[str] = mapped_column(String(256), default="")
    run_date: Mapped[date | None] = mapped_column(default=None)
    alert_key: Mapped[str] = mapped_column(String(64), default="")

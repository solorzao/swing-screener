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
    # freshness / anti-chase metric: (trigger_close - EMA20) / ATR at the trigger.
    # None for reversal plays (different geometry) and legacy rows.
    extension_atr: Mapped[float | None] = mapped_column(default=None)
    # streak start: the earliest run_date of the consecutive runs this (ticker,
    # timeframe, play_type) setup has been firing -- lets the surface age out repeats.
    # Equal to run_date for a freshly-appearing setup; None for legacy rows.
    first_seen_date: Mapped[date | None] = mapped_column(default=None)
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
    # which book this fill belongs to. "research" is the shadow grid (every screened
    # signal x arm x variant, auto-booked); a future curated "intent" book paper-executes
    # OrderIntents under "paper". The closed-trade research aggregates (leaderboards +
    # analyst calibration) are pinned to "research" so the intent book never inflates
    # them; open-trade stepping stays inclusive so paper trades still advance.
    account: Mapped[str] = mapped_column(String(16), default="research", index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), default=None)
    signal_score: Mapped[float]
    rank: Mapped[int]
    mtf_aligned: Mapped[bool] = mapped_column(default=False)
    # experiment arm: every fill is duplicated once per arm (same entry economics,
    # different exit management) so breakdown(trades, "arm") gives a same-sample A/B
    # of e.g. all-or-nothing ("baseline") vs a conditional partial ("partial33_cond").
    arm: Mapped[str] = mapped_column(String(32), default="baseline", index=True)
    # screen variant: the ENTRY/screen config that produced this fill -- the orthogonal
    # complement to `arm`. "default" is the live screen config; other variants re-screen
    # the prior bar under a tweaked StrategyConfig (e.g. a tighter freshness gate) and are
    # booked under the baseline exit, so breakdown(trades, "variant") is a strategy
    # leaderboard. Unlike arms, variants are NOT same-sample (different entries).
    variant: Mapped[str] = mapped_column(String(32), default="default", index=True)
    # categorization tags denormalized from the signal so the shadow book can be
    # sliced by them in QC without a join back to the (run-date-scoped) signal row.
    quality_tier: Mapped[str] = mapped_column(String(32), default="")
    volatility_tier: Mapped[str] = mapped_column(String(32), default="")
    oversold: Mapped[bool] = mapped_column(default=False)
    # broad market regime at fill time (SPY proxy), for performance attribution:
    # market_trend "bull"/"bear" (vs 200DMA), market_vol "calm"/"elevated"/"high" (ATR%).
    # None = unknown (SPY data unavailable) or a legacy row.
    market_trend: Mapped[str | None] = mapped_column(String(16), default=None)
    market_vol: Mapped[str | None] = mapped_column(String(16), default=None)
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
    # fractional-close support: the first leg is scaled out at the target, the
    # runner then trails to stop/flip/time. realized_r becomes a size-weighted
    # blend of the booked partial and the runner's final R.
    partial_done: Mapped[bool] = mapped_column(default=False)
    partial_price: Mapped[float | None] = mapped_column(default=None)
    partial_r: Mapped[float | None] = mapped_column(default=None)
    remaining_frac: Mapped[float] = mapped_column(default=1.0)
    high_water: Mapped[float | None] = mapped_column(default=None)


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


class AnalystCall(Base):
    """One persisted analyst conviction call, for the learning/calibration loop.

    Every time the Opus analyst nudges the deterministic baseline conviction we
    record the call: the pick keys, both convictions, the nudge reason, and the
    model. ``realized_r`` / ``scored_at`` start NULL -- a later pass grades how the
    nudge actually played out, closing the calibration loop. ``ticker`` is indexed
    for per-name lookups; strings stay bounded so Azure SQL can index them.
    """

    __tablename__ = "analyst_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_date: Mapped[date]
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(32))
    play_type: Mapped[str] = mapped_column(String(16))
    run_date: Mapped[date]
    baseline_conviction: Mapped[str] = mapped_column(String(16))
    final_conviction: Mapped[str] = mapped_column(String(16))
    nudge_reason: Mapped[str] = mapped_column(String(512))
    model: Mapped[str] = mapped_column(String(64))
    # to-be-scored by the calibration loop: NULL until the outcome is graded.
    realized_r: Mapped[float | None] = mapped_column(default=None)
    scored_at: Mapped[date | None] = mapped_column(default=None)


class ExecutionLog(Base):
    """One append-only execution record, shared by all Phase 3 execution adapters.

    This single table does quadruple duty: (1) the IDEMPOTENCY guard -- a unique
    ``idempotency_key`` per intent x run so a force-resent or hourly-digest re-run
    never double-submits the same order; (2) the AUDIT trail of every adapter
    decision; (3) the SOURCE for the hard-limit sums (per-day notional / loss),
    which read the rows whose ``status`` counts against the limits; and (4) the
    order ticket the ``manual`` adapter records. ``ticker`` is indexed for per-name
    lookups; every string column is bounded so Azure SQL can index it
    (NVARCHAR(max) is un-indexable).
    """

    __tablename__ = "execution_logs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_execution_logs_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_date: Mapped[date]
    # pick keys: which signal/intent this execution record belongs to.
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(32))
    play_type: Mapped[str] = mapped_column(String(16))
    run_date: Mapped[date]
    # which book + adapter mode produced the record.
    account: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16))
    # the order spec (the ticket).
    side: Mapped[str] = mapped_column(String(8))
    limit_price: Mapped[float]
    shares: Mapped[int]
    stop: Mapped[float]
    target: Mapped[float]
    risk_dollars: Mapped[float]
    notional: Mapped[float]
    # the outcome: recorded / filled_paper / skipped / rejected, plus a human detail.
    status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str] = mapped_column(String(512))
    # the idempotency guard: unique per intent x run (see uq above).
    idempotency_key: Mapped[str] = mapped_column(String(64))


class AnalysisRequest(Base):
    """Queue row for an on-demand single-ticker deep-analysis report."""

    __tablename__ = "analysis_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    requested_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    recipient: Mapped[str] = mapped_column(String(256), default="")
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    summary: Mapped[str] = mapped_column(String(512), default="")
    pdf_blob_key: Mapped[str | None] = mapped_column(String(512), default=None)
    chart_blob_keys: Mapped[str] = mapped_column(String(2048), default="")
    error: Mapped[str | None] = mapped_column(String(1024), default=None)

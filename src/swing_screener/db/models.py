"""SQLAlchemy 2.x typed declarative models for the screener's local store.

The same models are intended to later target Azure SQL, so they stick to
portable column types. Nullable fields are declared as ``Mapped[T | None]``
with ``default=None``.
"""

from datetime import date, datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
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
    # GICS sector (yfinance-populated), for the daily diversity cap. None = unknown.
    sector: Mapped[str | None] = mapped_column(String(64), default=None)


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
    # conviction tier (reversal): premium / strong / base -- tiered surfacing + sizing.
    conviction_tier: Mapped[str] = mapped_column(String(16), default="base")
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
    # conviction tier (reversal): premium / strong / base -- drives conviction sizing.
    conviction_tier: Mapped[str] = mapped_column(String(16), default="base")
    # which book this fill belongs to. "research" is the shadow grid (every screened
    # signal x arm x variant, auto-booked); a future curated "intent" book paper-executes
    # OrderIntents under "paper". The closed-trade research aggregates (leaderboards +
    # analyst calibration) are pinned to "research" so the intent book never inflates
    # them; open-trade stepping stays inclusive so paper trades still advance.
    account: Mapped[str] = mapped_column(String(16), default="research", index=True)
    # the trigger bar's timestamp (the completed bar the signal fired on), for CROSS-RUN
    # dedup: a weekly flip bar is re-detected as a "prior signal" on every daily run of
    # the week, so without this key each setup was booked 5-12 times (2026-07 audit).
    # The shadow book books each distinct trigger ONCE per (ticker, timeframe, play_type,
    # variant). None on legacy rows and test-seam candidates (no dedup applied).
    trigger_ts: Mapped[datetime | None] = mapped_column(default=None)
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
    # VIX percentile-rank bucket at fill time (trailing 252d): low/<40, mid/40-70, high/>70.
    # None = unknown (^VIX unavailable) or a legacy row. Used by breakdown(trades,"vix_bucket").
    vix_bucket: Mapped[str | None] = mapped_column(String(16), default=None)
    # would this signal have SURFACED in the digest (booking-time estimate: strength +
    # top-5 rank under the live surfacing config)? North Star #7: the gold forward book
    # must reflect entries a human could actually take -- reflection grades only
    # would_surface=True rows, the rest are the research facet. None = legacy row
    # (pre-stamping) or a replay row (ticker-major replay ranks are degenerate; see
    # pipeline.run._shadow_candidates). Neither is ever graded as gold.
    would_surface: Mapped[bool | None] = mapped_column(default=None)
    # lowest low since the fill (MAE instrumentation, mirroring high_water). Never read
    # by any exit decision -- purely so max-adverse-excursion questions (stop width,
    # breakeven timing, target reachability) are answerable offline. None = legacy row.
    low_water: Mapped[float | None] = mapped_column(default=None)
    # filled / missed / invalidated / pending (a reversal resting-limit order still
    # inside its fill window; resolve_pending fills, invalidates, or expires it).
    fill_status: Mapped[str] = mapped_column(String(32))
    # the entry zone, persisted for PENDING rows so later window bars can resolve the
    # fill without the (transient) screen-time EntryZone. None on legacy rows.
    entry_floor: Mapped[float | None] = mapped_column(default=None)
    entry_ceiling: Mapped[float | None] = mapped_column(default=None)
    # bars checked while pending (the fill-window counter). None = never pended.
    pending_bars: Mapped[int | None] = mapped_column(default=None)
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
    # which book the closing trade belonged to, mirroring ``PaperTrade.account``.
    # The shadow stepper records EVERY paper exit under ``is_paper=True`` -- both the
    # research grid ("research") and the curated intent book ("paper") -- so ``is_paper``
    # alone can't separate them. This display-only facet lets the exit log be sliced by
    # book. Defaults to "research" so existing rows backfill to the research grid;
    # bounded + indexed for Azure SQL, matching PaperTrade.account.
    account: Mapped[str] = mapped_column(String(16), default="research", index=True)
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
    # APPROXIMATE token spend captured from the Opus call (see notify.analysis.Usage):
    # the raw token counts, the web-search count, and the list-price cost estimate that
    # feeds the run-cost safety cap. All NULL on the deterministic/fallback path (no model
    # call was made) and on legacy rows -- nullable, no server_default needed.
    input_tokens: Mapped[int | None] = mapped_column(default=None)
    output_tokens: Mapped[int | None] = mapped_column(default=None)
    web_searches: Mapped[int | None] = mapped_column(default=None)
    est_cost_usd: Mapped[float | None] = mapped_column(default=None)
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
    # the outcome: recorded / filled_paper / skipped / rejected, plus the Phase 4 live
    # statuses (submitted_live / filled_live / canceled / rejected_live), and a human detail.
    status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str] = mapped_column(String(512))
    # the idempotency guard: unique per intent x run (see uq above).
    idempotency_key: Mapped[str] = mapped_column(String(64))
    # Phase 4 live-broker tracking: the broker name (e.g. "alpaca"; "" for non-broker rows
    # like manual / paper), the broker's order id, and the broker-reported status. The two
    # id columns are NULL until a live order is actually placed; all three are indexed for
    # per-broker / per-order lookups, so they stay bounded (Azure SQL can't index NVARCHAR(max)).
    broker: Mapped[str] = mapped_column(String(16), default="", index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    broker_status: Mapped[str | None] = mapped_column(String(32), default=None)


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


class MarketReport(Base):
    """One weekly macro "Market Weather" snapshot: the deterministic market facts + the analyst's
    read. A history of how the market moved (and a flip log) over time."""

    __tablename__ = "market_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    # unique: ONE report per as-of date -- the idempotency backstop against the DST
    # double-fire / crash-retry duplicates observed 2026-06 (migration f4c1e8a2b6d9).
    run_date: Mapped[date] = mapped_column(index=True, unique=True)
    ha_alignment: Mapped[str] = mapped_column(String(16))  # aligned_bull / aligned_bear / mixed
    flipped: Mapped[bool] = mapped_column(default=False)   # any SPY timeframe flipped this run
    spy_vs_200dma: Mapped[str | None] = mapped_column(String(8), default=None)
    vol_bucket: Mapped[str | None] = mapped_column(String(16), default=None)
    vix: Mapped[float | None] = mapped_column(default=None)
    vix_rank: Mapped[float | None] = mapped_column(default=None)
    vix_spike: Mapped[bool] = mapped_column(default=False)
    ten_year: Mapped[float | None] = mapped_column(default=None)
    three_month: Mapped[float | None] = mapped_column(default=None)
    yield_inverted: Mapped[bool | None] = mapped_column(default=None)
    bond_trend: Mapped[str | None] = mapped_column(String(8), default=None)
    # v2 cross-asset signals
    vix_term_ratio: Mapped[float | None] = mapped_column(default=None)
    vix_backwardation: Mapped[bool] = mapped_column(default=False)
    credit_chg_4w: Mapped[float | None] = mapped_column(default=None)
    credit_pctile: Mapped[float | None] = mapped_column(default=None)
    cyc_def_trend: Mapped[str | None] = mapped_column(String(8), default=None)
    cyc_def_chg_4w: Mapped[float | None] = mapped_column(default=None)
    breadth_trend: Mapped[str | None] = mapped_column(String(8), default=None)
    breadth_chg_4w: Mapped[float | None] = mapped_column(default=None)
    recession_prob: Mapped[float | None] = mapped_column(default=None)
    is_deep: Mapped[bool] = mapped_column(default=False)   # True when the LLM analyst produced it
    core: Mapped[str] = mapped_column(String(512), default="")
    report: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime | None] = mapped_column(default=None)


class ReversalFunnel(Base):
    """One row per daily digest: the reversal funnel snapshot (detected -> confirmed ->
    fresh -> actionable -> surfaced), plus the config bars that did the filtering.

    fresh/actionable/surfaced are DIGEST-TIME state (repeat cooldown vs the signals
    history, live quotes for the already-ran drop, sector cap) and are UNRECOVERABLE
    later -- this table is the only record. detected/confirmed are recomputable from
    signals only until a re-screen rewrites the run_date. Strings stay bounded so
    Azure SQL can index them (NVARCHAR(max) is un-indexable)."""

    __tablename__ = "reversal_funnels"

    id: Mapped[int] = mapped_column(primary_key=True)
    # unique: ONE funnel row per run_date -- a forced digest resend re-executes the
    # write, which delete-then-inserts (migration d7e4b2f9a1c6 adds the uq index).
    run_date: Mapped[date] = mapped_column(index=True, unique=True)
    detected: Mapped[int]                                # all reversal signals stored
    confirmed: Mapped[int]                               # strength == "confirmed"
    fresh: Mapped[int]                                   # strength bar + cooldown + pool cap
    actionable: Mapped[int]                              # after the already-ran drop
    surfaced: Mapped[int]                                # after the sector cap + top-5
    # comma-joined tickers that cleared every bar but lost the top-5/sector race.
    overflow_tickers: Mapped[str] = mapped_column(String(512), default="")
    # the config bars in force when the snapshot was taken.
    pool_n: Mapped[int] = mapped_column(default=20)
    confirmed_only: Mapped[bool] = mapped_column(default=True)
    premium_only: Mapped[bool] = mapped_column(default=False)
    # seam wired at digest time (a live-quote fn was injected) -- NOT "the check ran":
    # _drop_already_ran fails open on a quote outage, so True can coexist with
    # actionable == fresh on an outage day.
    already_ran_checked: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime | None] = mapped_column(default=None)


class GexSnapshot(Base):
    """One computed GEX map per (underlying, snapshot time). Options-lab table:
    lifecycle columns are DateTime, not Date -- the lab trades inside the session
    (see docs/modules/gex-lab.md; equity tables stay day-keyed)."""

    __tablename__ = "gex_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    spot: Mapped[float]
    call_wall: Mapped[float | None] = mapped_column(default=None)
    put_wall: Mapped[float | None] = mapped_column(default=None)
    gamma_flip: Mapped[float | None] = mapped_column(default=None)
    net_gex: Mapped[float | None] = mapped_column(default=None)
    regime: Mapped[str] = mapped_column(String(16), default="unknown")
    # Per-strike profile as JSON. Text, not a bounded string: a dense SPY chain
    # exceeds any indexable bound, and this column is display-only (never filtered).
    profile_json: Mapped[str] = mapped_column(Text, default="[]")
    thin_chain: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(16), default="computed")


class OptionSetup(Base):
    """One journaled setup, graded against the 12-point A+ checklist at decision time."""

    __tablename__ = "option_setups"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # long | short (calls vs puts)
    gex_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("gex_snapshots.id"), default=None
    )
    regime: Mapped[str] = mapped_column(String(16), default="unknown")
    pivot_level: Mapped[float | None] = mapped_column(default=None)
    pattern: Mapped[str] = mapped_column(String(256), default="")
    entry: Mapped[float | None] = mapped_column(default=None)
    stop: Mapped[float | None] = mapped_column(default=None)
    target: Mapped[float | None] = mapped_column(default=None)
    chk_daily_bias_clear: Mapped[bool] = mapped_column(default=False)
    chk_daily_stack_ordered: Mapped[bool] = mapped_column(default=False)
    chk_m5_agrees: Mapped[bool] = mapped_column(default=False)
    chk_gex_levels_marked: Mapped[bool] = mapped_column(default=False)
    chk_price_at_pivot: Mapped[bool] = mapped_column(default=False)
    chk_regime_match: Mapped[bool] = mapped_column(default=False)
    chk_pattern_clean: Mapped[bool] = mapped_column(default=False)
    chk_volume_confirming: Mapped[bool] = mapped_column(default=False)
    chk_risk_sized: Mapped[bool] = mapped_column(default=False)
    chk_stop_structural: Mapped[bool] = mapped_column(default=False)
    chk_rr_at_least_2: Mapped[bool] = mapped_column(default=False)
    chk_confirmation_candle: Mapped[bool] = mapped_column(default=False)
    grade: Mapped[str] = mapped_column(String(8), default="no_trade")
    status: Mapped[str] = mapped_column(String(16), default="idea", index=True)
    notes: Mapped[str] = mapped_column(String(2048), default="")


class OptionPaperTrade(Base):
    """One options-lab trade: a paper trade opened from a setup, or one imported
    Robinhood flat-to-flat episode. Never mixed with the equity paper_trades table."""

    __tablename__ = "option_paper_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    setup_id: Mapped[int | None] = mapped_column(ForeignKey("option_setups.id"), default=None)
    account: Mapped[str] = mapped_column(String(16), default="options-lab", index=True)
    strategy: Mapped[str] = mapped_column(String(16), default="gex", index=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(8), default="long")
    opened_at: Mapped[datetime | None] = mapped_column(default=None)
    closed_at: Mapped[datetime | None] = mapped_column(default=None)
    entry: Mapped[float | None] = mapped_column(default=None)
    stop: Mapped[float | None] = mapped_column(default=None)
    target: Mapped[float | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    exit_price: Mapped[float | None] = mapped_column(default=None)
    exit_reason: Mapped[str | None] = mapped_column(String(16), default=None)
    realized_r: Mapped[float | None] = mapped_column(default=None)
    hold_minutes: Mapped[int | None] = mapped_column(default=None)
    # Imported-episode fields (nullable for paper trades). OCC symbols are 21 chars.
    occ_symbol: Mapped[str | None] = mapped_column(String(24), default=None, index=True)
    strike: Mapped[float | None] = mapped_column(default=None)
    expiry: Mapped[date | None] = mapped_column(default=None)
    right: Mapped[str | None] = mapped_column(String(4), default=None)
    contracts: Mapped[int | None] = mapped_column(default=None)
    entry_premium: Mapped[float | None] = mapped_column(default=None)
    exit_premium: Mapped[float | None] = mapped_column(default=None)
    premium_pnl: Mapped[float | None] = mapped_column(default=None)
    # Idempotency for import commits: hash of the episode's first fill. Unique so
    # re-committing the same review is a no-op, nullable so paper trades skip it.
    import_key: Mapped[str | None] = mapped_column(String(64), default=None, unique=True)
    needs_review: Mapped[bool] = mapped_column(default=False)


class BrokerFill(Base):
    """One immutable imported broker transaction (Robinhood activity CSV line).
    Fills persist even when their episode is skipped at review -- dedup and
    re-review both need them; episode pairing is re-runnable from fills."""

    __tablename__ = "broker_fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_hash: Mapped[str] = mapped_column(String(64), unique=True)
    activity_date: Mapped[date] = mapped_column(index=True)
    underlying: Mapped[str] = mapped_column(String(16), index=True)
    occ_symbol: Mapped[str] = mapped_column(String(24), index=True)
    trans_code: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int]
    price: Mapped[float | None] = mapped_column(default=None)
    amount: Mapped[float | None] = mapped_column(default=None)
    raw: Mapped[str] = mapped_column(String(1024), default="")
    source: Mapped[str] = mapped_column(String(16), default="robinhood")

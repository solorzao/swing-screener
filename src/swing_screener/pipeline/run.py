import argparse
import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from swing_screener.charts.render import render_chart
from swing_screener.config import StrategyConfig
from swing_screener.data.fetch import (
    avg_dollar_volume,
    fetch_bars,
    fetch_market_cap,
    fetch_sector,
)
from swing_screener.data.resample import resample_ohlcv
from swing_screener.data.universe import load_universe
from swing_screener.db import repo
from swing_screener.db.models import Signal
from swing_screener.db.session import get_engine
from swing_screener.notify.select import REVERSAL_POOL_N
from swing_screener.pipeline.analyze import (
    SignalResult,
    analyze_frames,
    analyze_reversals,
    build_frames,
)
from swing_screener.pipeline.arms import BASELINE, build_arms
from swing_screener.pipeline.broker import BrokerClient
from swing_screener.pipeline.broker_alpaca import build_broker
from swing_screener.pipeline.diversity import cap_by_sector, first_per_ticker
from swing_screener.pipeline.reconcile import reconcile_live
from swing_screener.pipeline.regime import MARKET_PROXY, classify_regime
from swing_screener.pipeline.shadow import (
    FillCandidate,
    advance_open,
    open_from_signals,
    resolve_pending,
)
from swing_screener.pipeline.variants import DEFAULT_VARIANT, build_screen_variants
from swing_screener.settings import load_settings
from swing_screener.storage.blob import blob_enabled, upload_chart

log = logging.getLogger(__name__)

_BAR_KEYS = ("low", "high", "close", "shaved_head", "bearish", "shaved_bottom", "atr")

# Calendar timeframes resample with the IN-PROGRESS bucket as the frame's last bar
# (see data.resample) -- the shadow book must never read that partial bar.
_CALENDAR_TFS = ("1wk", "1mo")


def _is_period_end(today: date, timeframe: str) -> bool:
    """True when a calendar bucket (1wk/1mo) completes at today's close.

    1wk completes Friday; 1mo on the month's last business day (weekend-aware; a
    holiday-shortened period is missed and self-heals on the next run via the
    completed-bar label). Intraday/daily timeframes are always "complete" -- the
    evening screen runs after the close.
    """
    if timeframe == "1wk":
        return today.weekday() == 4
    if timeframe == "1mo":
        nxt = today + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt.month != today.month
    return True


def _latest_completed_bar(
    frame: pd.DataFrame, timeframe: str, today: date
) -> dict[str, float | bool | date] | None:
    """The last COMPLETED bar's row (per ``_bar_row``) stamped with its ``bar_date`` label,
    or None when only a partial bucket exists.

    Mid-period, a calendar frame's last bar is the in-progress bucket, so the completed
    bar is the one before it; on the period-end run the last bar completes today.
    ``advance_open`` keys idempotency on ``bar_date``, so open 1wk/1mo trades advance
    once per completed bar of their own timeframe -- not once per daily run against a
    half-formed candle (2026-07 audit)."""
    f = frame
    if timeframe in _CALENDAR_TFS and not _is_period_end(today, timeframe):
        if len(frame) < 2:
            return None
        f = frame.iloc[:-1]
    row: dict[str, float | bool | date] = dict(_bar_row(f))
    row["bar_date"] = f.index[-1].date()
    return row

# Azure SQL serverless error raised while the database is auto-resuming from a
# paused state: the first connection of the day fails with this until the DB
# wakes (~1 min). Retried (not fatal); any OTHER error is a real misconfig.
_SERVERLESS_RESUMING = "40613"


def _alembic_dir() -> Path:
    """Directory holding ``alembic.ini`` + ``alembic/``.

    NOT relative to this file: in a non-editable install (the container) the
    package lives in ``site-packages`` while the Dockerfile copies the migration
    files to the WORKDIR (``/app``). So resolve from ``SWING_ALEMBIC_DIR`` (set to
    ``/app`` in the image) or the current working directory (the repo root
    locally, the WORKDIR in the container) -- never the package location.
    """
    return Path(os.environ.get("SWING_ALEMBIC_DIR", ".")).resolve()


def _alembic_upgrade(db_url: str) -> None:
    """Run ``alembic upgrade head`` against ``db_url`` (the live path).

    Alembic -- not ``create_all`` -- owns the Azure SQL schema, so the pipeline
    must upgrade before it screens. The import is LAZY because alembic ships in
    the optional ``azure`` extra and is absent in CI / local sqlite runs; tests
    monkeypatch this whole function so it never executes there.
    """
    from alembic.config import Config

    from alembic import command

    base = _alembic_dir()
    cfg = Config(str(base / "alembic.ini"))
    cfg.set_main_option("script_location", str(base / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def _migrate_with_retry(
    db_url: str,
    *,
    attempts: int = 5,
    sleep_fn: Callable[[float], None] = time.sleep,
    upgrade_fn: Callable[[str], None] | None = None,
) -> None:
    """Run the migration, tolerating Azure SQL serverless cold-resume.

    A paused serverless DB rejects the first connection with error 40613 while
    it wakes; we back off and retry up to ``attempts``. Any other failure is a
    real problem (bad URL, auth, broken migration) and is re-raised IMMEDIATELY
    with no retry. ``sleep_fn``/``upgrade_fn`` are injectable so tests run
    instantly and fully offline.
    """
    upgrade = upgrade_fn or _alembic_upgrade
    for attempt in range(1, attempts + 1):
        try:
            upgrade(db_url)
            return
        except Exception as exc:
            if _SERVERLESS_RESUMING not in str(exc):
                raise  # not a resume; fail fast on the real error
            if attempt == attempts:
                raise  # exhausted: surface the last 40613 to the caller
            backoff = 2.0 * attempt
            log.warning(
                "db resuming (40613); migration attempt %d/%d failed, retrying in %.0fs",
                attempt, attempts, backoff,
            )
            sleep_fn(backoff)


@dataclass(frozen=True)
class RunResult:
    n_signals: int      # continuation + reversal signals persisted
    n_paper_opened: int
    n_charts: int
    n_failed: int
    n_reversals: int = 0


# Cadences that select per-timeframe rather than from the global ranking. The
# weekly/monthly digests pick the top-N within these timeframes, so such a pick
# can rank below the global top-N -- it must still be charted or the digest shows
# it with no chart. Mirrors notify.select.{weekly,monthly}_picks.
_DIGEST_TIMEFRAMES = ("1wk", "1mo")


# Over-render margin: the digest's ALREADY-RAN drop and sector-cap skew (live quotes /
# live-fetched sectors -- digest-time state the evening render genuinely can't see)
# still promote lower-ranked names past any deterministic selection. A few extra charts
# per cadence absorb those promotions (deliberate small waste; a promoted pick arriving
# chartless is worse). The COOLDOWN half of the gap is deterministic at screen time and
# handled exactly by the fresh-first union below (2026-07-26: 27.5% of analyzed picks
# over 5 weeks shipped chartless at booking, almost all cooldown promotions).
_CHART_MARGIN = 5

# Freshness horizons for the slow cadences, in SCREEN RUNS. Kept in sync with
# notify.run._COOLDOWN_RUNS (weekly=5, monthly=21) the same way _SURFACE_TOP_N is
# kept in sync with notify.select's top_n default.
_TF_COOLDOWN_RUNS = {"1wk": 5, "1mo": 21}


def _fresh_predicate(
    recent_run_dates: "Sequence[date]", horizon_runs: int | None,
    first_seen_of: "Callable[[SignalResult], date | None]",
) -> "Callable[[SignalResult], bool]":
    """Screen-time mirror of ``notify.select._fresh_enough``: will the digest still
    call this signal fresh after ``horizon_runs`` screen runs? Counts over the
    distinct run_dates actually stored (``recent_run_dates``, newest first, with
    today's run already saved), NOT calendar days. Fail-open like the digest:
    NULL first_seen, a disabled cooldown (None), or a store younger than the
    window all read as fresh."""
    if horizon_runs is None:
        return lambda r: True
    runs = list(recent_run_dates)[: horizon_runs + 1]
    if not runs:
        return lambda r: True
    cutoff = runs[-1]

    def fresh(r: "SignalResult") -> bool:
        fs = first_seen_of(r)
        return fs is None or fs >= cutoff

    return fresh


def _digest_chart_indices(
    results: list[SignalResult], top_n: int, *,
    sector_of: "Callable[[SignalResult], str | None] | None" = None,
    max_per_sector: int | None = None,
    first_seen_of: "Callable[[SignalResult], date | None] | None" = None,
    recent_run_dates: "Sequence[date] | None" = None,
    daily_cooldown_runs: int | None = None,
) -> list[int]:
    """Indices into score-sorted ``results`` for every signal a digest can pick.

    Charts are rendered for the UNION of the global top-N (the daily digest) and
    the top-N within each per-timeframe cadence (weekly=1wk, monthly=1mo) -- each
    extended by ``_CHART_MARGIN`` to cover already-ran/sector-skew promotions.
    Without the per-timeframe slices a weekly/monthly pick ranked below the global
    top-N would reach the digest with no chart. ``results`` is sorted by score
    descending, so a timeframe's first ``top_n`` entries are exactly its picks.
    Kept in sync with notify.select, whose pickers all default to top_n=3.

    When ``first_seen_of``/``recent_run_dates`` are given, every slice is ALSO
    computed over the FRESH-only ordering -- the signals the next digest's
    staleness cooldown will actually keep (``daily_cooldown_runs`` for the daily
    slice, ``_TF_COOLDOWN_RUNS`` for the cadences) -- and unioned in. The cooldown
    is deterministic at screen time, so this closes the promoted-past-the-margin
    gap exactly (2026-07-26 measurement: 27.5% of analyzed picks chartless at
    booking, dominated by stale raw leaders crowding the charted set); the margin
    now only has to absorb the genuinely unpredictable drops (already-ran, live
    sector skew).

    The daily slice is chosen TICKER-wise, mirroring daily_picks' per-ticker dedup:
    first (best-scored) row per ticker, then the sector cap / prefix picks ``depth``
    DISTINCT tickers. Walking raw rows instead let one name's multi-timeframe dups
    consume chart (and sector) slots the digest no longer gives them -- the pick the
    digest promoted into the freed slot shipped chartless (its Opus call ran with
    no vision input).

    When ``max_per_sector``/``sector_of`` are given the daily slice mirrors
    notify.select.daily_picks' sector cap, so a pick promoted into the daily list by
    the cap is charted (and one capped far OUT isn't needlessly rendered).
    """
    depth = top_n + _CHART_MARGIN
    indexed = list(enumerate(results))

    def _daily_tickers(pool: "list[tuple[int, SignalResult]]") -> set[str]:
        deduped = first_per_ticker(pool, lambda p: p[1].ticker)
        if max_per_sector is not None and sector_of is not None:
            chosen = cap_by_sector(deduped, lambda p: sector_of(p[1]),
                                   max_per_sector=max_per_sector, limit=depth)
        else:
            chosen = deduped[:depth]  # global top-N DISTINCT tickers (daily digest)
        return {p[1].ticker for p in chosen}

    tickers = _daily_tickers(indexed)
    if first_seen_of is not None and recent_run_dates is not None:
        fresh = _fresh_predicate(recent_run_dates, daily_cooldown_runs, first_seen_of)
        tickers |= _daily_tickers([p for p in indexed if fresh(p[1])])
    # Chart EVERY row of a chosen ticker (<=4, one per timeframe), not just the row
    # that won the slot: a digest-time cooldown drop of the ticker's best row
    # promotes its other-timeframe row, which a row-wise dedup would leave unrendered.
    idx = {i for i, r in indexed if r.ticker in tickers}
    for tf in _DIGEST_TIMEFRAMES:
        tf_indices = [i for i, r in indexed if r.timeframe == tf]
        idx.update(tf_indices[:depth])
        if first_seen_of is not None and recent_run_dates is not None:
            fresh_tf = _fresh_predicate(
                recent_run_dates, _TF_COOLDOWN_RUNS.get(tf), first_seen_of)
            idx.update([i for i in tf_indices if fresh_tf(results[i])][:depth])
    return sorted(idx)


def _fetch_all_timeframes(ticker: str, *, cache_dir: Path, today: date,
                          cfg: StrategyConfig) -> dict[str, pd.DataFrame]:
    """Fetch + resample the four timeframes for one ticker. Thin glue over the
    (already-tested) fetch + resample layers; mocked in tests."""
    out: dict[str, pd.DataFrame] = {}
    hourly = fetch_bars(ticker, "1h", cache_dir=cache_dir, today=today, period="60d")
    if hourly is not None and not hourly.empty:
        out["4h"] = resample_ohlcv(hourly, "4h")
    daily = fetch_bars(ticker, "1d", cache_dir=cache_dir, today=today)
    if daily is not None and not daily.empty:
        out["1d"] = daily
        out["1wk"] = resample_ohlcv(daily, "1W")
        out["1mo"] = resample_ohlcv(daily, "1ME")
    return out


def _bar_row(frame: pd.DataFrame) -> dict[str, float | bool]:
    last = frame.iloc[-1]
    row: dict[str, float | bool] = {k: last[k] for k in _BAR_KEYS}
    # body_shrinking: the HA body is smaller than the prior bar's (momentum
    # decelerating) -- an input to the conditional-partial softening gate. The
    # exit machinery reads it off the bar, so it's computed here where the full
    # frame is in hand. False when there's no prior bar to compare against.
    row["body_shrinking"] = bool(
        len(frame) >= 2 and frame["body_frac"].iloc[-1] < frame["body_frac"].iloc[-2]
    )
    return row


# The digest's list depth (mirrors notify.select's top_n=3 default): the would_surface
# stamp calls a signal surfaced when it holds a top-3 rank within its play type.
# 2026-07-26: 5 -> 3 alongside the digest (research note: would_surface gold-book
# membership narrows from this run_date forward; earlier rows were stamped at 5).
_SURFACE_TOP_N = 3


def _passes_reversal_surface(pr: SignalResult, cfg: StrategyConfig) -> bool:
    """The digest's reversal strength/tier bars (mirrors ``notify.select.reversal_picks``):
    the filter half of surfacing, shared by the ``would_surface`` stamp and the chart
    renderer (both must agree with the digest about WHICH reversals can reach the email)."""
    if cfg.reversal_surface_premium_only and pr.conviction_tier != "premium":
        return False
    return not (cfg.reversal_surface_confirmed_only and pr.strength != "confirmed")


def _would_surface(pr: SignalResult, rank: int, cfg: StrategyConfig) -> bool:
    """Booking-time estimate of "would the digest have surfaced this signal?".

    North Star #7: the gold forward book must reflect entries a human could actually
    take, so reflection grades only stamped-True rows. Applies the surfacing config's
    strength/tier bars and the top-N rank within the play type. Deliberately
    APPROXIMATE: the cooldown, the live already-ran check, and the sector cap depend on
    digest-time state that does not exist at booking -- the stamp is the booking-time
    upper bound of surfacing, which still removes the ~92% hidden-EARLY + rank-6+ bulk
    that made the old full-book verdicts unrepresentative (2026-07 review).

    Continuation PARKING: with ``surface_continuation`` False (the default since the Q6
    NULL completed the falsification, 2026-07-25 -- docs/plans/
    2026-07-25-q6-q7-sweep-results.md) no continuation signal surfaces anywhere, so the
    stamp goes falsy for ALL of them; the shadow rows still book (detection/scoring/
    booking are untouched -- only what the gold facet counts as tradable changes).
    """
    if pr.play_type == "continuation" and not cfg.surface_continuation:
        return False
    if pr.play_type == "reversal" and not _passes_reversal_surface(pr, cfg):
        return False
    return rank <= _SURFACE_TOP_N


def _reversal_chart_indices(results: list[SignalResult], cfg: StrategyConfig, *,
                            top_n: int, pool_n: int,
                            first_seen_of: "Callable[[SignalResult], date | None] | None" = None,
                            recent_run_dates: "Sequence[date] | None" = None,
                            daily_cooldown_runs: int | None = None) -> list[int]:
    """Indices into score-sorted reversal ``results`` to chart.

    The digest picks CONFIRMED-only (per config) from a ``pool_n``-deep pool with an
    already-ran drop and a sector-cap backfill, so a surfaced pick can sit ANYWHERE in
    that pool -- while charts used to go to the raw top-``top_n`` by score, which EARLY
    signals dominated: most emailed reversal picks arrived as chartless PDF sections
    (2026-07-03 diagnosis: 4 of the 5 Jul-1 picks had no chart). Chart the digest-
    ELIGIBLE pool first, then top up with the best remaining signals to at least
    ``top_n`` so the dashboard still shows the leading raw signals when few are eligible.

    The pool is counted in DISTINCT tickers, mirroring reversal_picks' per-ticker
    dedup: the digest pool holds ONE slot per ticker and reaches past raw index
    ``pool_n`` when dups sit inside it, so the charted pool is the eligible rows of
    the first ``pool_n`` distinct tickers -- every row of an admitted ticker charts
    (a digest-time cooldown drop of its best row promotes the other-timeframe one).

    When ``first_seen_of``/``recent_run_dates`` are given, the pool walk ALSO runs
    over the FRESH-only eligible ordering and unions in -- the digest's own pool is
    fresh-first (``reversal_picks`` with ``max_age_days``), so stale eligible
    tickers crowding the raw walk used to push the picks the digest would actually
    surface past the charted budget (2026-07-09: all 5 emailed reversal picks
    chartless). With the union, the charted set is a superset of the digest's
    selectable pool by construction.
    """
    eligible = [i for i, r in enumerate(results) if _passes_reversal_surface(r, cfg)]

    def _pool_rows(candidates: list[int]) -> list[int]:
        pool_tickers: set[str] = set()
        rows: list[int] = []
        for i in candidates:
            ticker = results[i].ticker
            if ticker not in pool_tickers:
                if len(pool_tickers) >= pool_n:
                    continue  # past the pool's ticker budget (admitted names still chart)
                pool_tickers.add(ticker)
            rows.append(i)
        return rows

    chosen = set(_pool_rows(eligible))
    if first_seen_of is not None and recent_run_dates is not None:
        fresh = _fresh_predicate(recent_run_dates, daily_cooldown_runs, first_seen_of)
        chosen |= set(_pool_rows([i for i in eligible if fresh(results[i])]))
    idx = sorted(chosen)
    if len(idx) < top_n:
        idx += [i for i in range(len(results)) if i not in chosen][: top_n - len(idx)]
    return sorted(idx)


def _shadow_candidates(
    prior_list: list[tuple[SignalResult, float, float, datetime | None]],
    surface_cfg: StrategyConfig | None = None,
) -> tuple[list[FillCandidate], dict[tuple[str, str], tuple[float, float]]]:
    """Build the shadow-book fill candidates + next-bar map for one variant's prior signals.

    Ranks WITHIN each play_type (continuation and reversal each rank from 1) by score --
    a ranking space distinct from the persisted ``Signal.rank``. The next-bar high/low is
    the actual traded bar, so it's config-independent across variants. Each candidate
    carries its trigger bar's timestamp for the cross-run booking dedup.

    ``surface_cfg`` (the variant's screen config) turns on the ``would_surface`` stamp --
    meaningful only when ``prior_list`` spans the whole universe, so the LIVE screen
    passes it and the ticker-major replay leaves it None (a per-ticker walk ranks
    everything 1, which would stamp every confirmed signal surfaced).
    """
    prior_cont = sorted((x for x in prior_list if x[0].play_type == "continuation"),
                        key=lambda x: x[0].score, reverse=True)
    prior_rev = sorted((x for x in prior_list if x[0].play_type == "reversal"),
                       key=lambda x: x[0].score, reverse=True)
    candidates = [
        FillCandidate(pr.ticker, pr.timeframe, pr.horizon, pr.score, rank,
                      pr.mtf_aligned, None, pr.zone, quality_tier=pr.quality_tier,
                      volatility_tier=pr.volatility_tier, oversold=pr.oversold,
                      play_type=pr.play_type, strength=pr.strength,
                      conviction_tier=pr.conviction_tier, trigger_ts=trig,
                      would_surface=(_would_surface(pr, rank, surface_cfg)
                                     if surface_cfg is not None else None))
        for group in (prior_cont, prior_rev)
        for rank, (pr, _h, _l, trig) in enumerate(group, start=1)
    ]
    next_bars = {(pr.ticker, pr.timeframe): (h, low) for (pr, h, low, _t) in prior_list}
    return candidates, next_bars


def _to_signal(r: SignalResult, rank: int, run_date: date, first_seen: date) -> Signal:
    return Signal(
        run_date=run_date, ticker=r.ticker, timeframe=r.timeframe, horizon=r.horizon,
        play_type=r.play_type, strength=r.strength, conviction_tier=r.conviction_tier,
        score=r.score, rank=rank, mtf_aligned=r.mtf_aligned, quality_tier=r.quality_tier,
        volatility_tier=r.volatility_tier, oversold=r.oversold, trigger_close=r.trigger_close,
        atr=r.atr, rsi=r.rsi, entry_floor=r.entry_floor, entry_ceiling=r.entry_ceiling,
        stop=r.stop, target=r.target,
        extension_atr=r.extension_atr, first_seen_date=first_seen,
    )


def _render_and_attach(r: SignalResult, signal: Signal, chart_dir: Path, today: date) -> None:
    """Render ``r``'s chart and attach the path/blob-key to its ``signal`` row."""
    basename = f"{r.ticker}_{r.timeframe}_{today:%Y%m%d}.png"
    path = Path(chart_dir) / basename
    render_chart(r.frame, r.ctx, r.zone, path)
    if blob_enabled():
        # In Azure the filesystem is not shared across executions, so the PNG lives
        # in a private blob container; chart_path becomes the blob KEY.
        key = f"{today:%Y%m%d}/{basename}"
        upload_chart(path, key)
        signal.chart_path = key
    else:
        signal.chart_path = str(path)


def run_screen(*, universe_path: Path, db_url: str, cache_dir: Path, chart_dir: Path,
               top_charts: int = 3, cfg: StrategyConfig | None = None,
               today: date | None = None, max_tickers: int | None = None,
               migrate_fn: Callable[[str], None] | None = None,
               broker: BrokerClient | None = None) -> RunResult:
    cfg = cfg or StrategyConfig()
    today = today or datetime.now(UTC).date()
    # Live reconcile cadence: the BROKER owns live fills/exits, so when execution_mode=="live"
    # AND a broker is configured we reconcile the live book right where positions are advanced
    # (a fill materializes an account="live" PaperTrade; a venue close reconciles its exit).
    # GATED: off/paper or no broker -> the live book stays dark (no reconcile). The broker is
    # built from settings here (tests inject a FakeBroker via the `broker=` seam).
    settings = load_settings()
    if broker is None and settings.execution_mode == "live":
        broker = build_broker(settings)

    # Azure SQL: Alembic owns the schema, so upgrade to head BEFORE any engine
    # use (sqlite still goes through get_engine, which create_all's). Done up
    # front so a misconfigured/asleep DB fails before we spend time fetching.
    if db_url.startswith("mssql"):
        (migrate_fn or _migrate_with_retry)(db_url)

    # Persist the FULL seed up front (independent of max_tickers) so the universe
    # table mirrors the whole watchlist; metrics are filled in by the per-ticker
    # loop below and applied in one batch before the run ends.
    full_universe = load_universe(universe_path)
    engine = get_engine(db_url)
    with Session(engine) as s:
        repo.sync_universe(s, full_universe)
    universe = full_universe[:max_tickers] if max_tickers is not None else full_universe

    universe_metrics: dict[str, dict[str, float | str | None]] = {}
    today_results: list[SignalResult] = []      # continuation
    today_reversals: list[SignalResult] = []     # reversal
    # (prior signal, next_high, next_low, trigger bar timestamp)
    prior: list[tuple[SignalResult, float, float, datetime | None]] = []
    latest_bars: dict[tuple[str, str], dict[str, float | bool | date]] = {}

    # Screen-variant leaderboard: re-screen the prior bar under each alt config (they share
    # the base's indicator periods, so the same enriched frames are reused) and book each
    # variant's fills separately under the baseline exit. prior_variants mirrors `prior`.
    screen_variants = build_screen_variants(cfg)
    alt_variants = {n: c for n, c in screen_variants.items() if n != DEFAULT_VARIANT}
    prior_variants: dict[str, list[tuple[SignalResult, float, float, datetime | None]]] = {
        n: [] for n in alt_variants
    }

    n_failed = 0
    for entry in universe:
        try:
            bars_by_tf = _fetch_all_timeframes(entry.ticker, cache_dir=cache_dir,
                                               today=today, cfg=cfg)
            if not bars_by_tf:
                continue
            daily = bars_by_tf.get("1d")
            universe_metrics[entry.ticker] = {
                "avg_dollar_volume": avg_dollar_volume(daily) if daily is not None else None,
                "market_cap": fetch_market_cap(entry.ticker, cache_dir=cache_dir, today=today),
                "sector": fetch_sector(entry.ticker, cache_dir=cache_dir, today=today),
            }
            frames = build_frames(bars_by_tf, cfg)
            for tf, f in frames.items():
                if len(f):
                    # Only COMPLETED bars reach the bar-stepper: mid-period, a calendar
                    # frame's last bar is the in-progress bucket, and advancing on it
                    # denominated 1wk holds in trading DAYS (2026-07 audit).
                    row = _latest_completed_bar(f, tf, today)
                    if row is not None:
                        latest_bars[(entry.ticker, tf)] = row
            today_results.extend(analyze_frames(entry.ticker, frames, cfg))
            today_reversals.extend(analyze_reversals(entry.ticker, frames, cfg))
            # Prior-bar signals (both play types) feed the shadow book: a signal that
            # fired on the prior bar is filled if today's bar trades into its zone.
            # Calendar timeframes book ONLY on their period-end run, when the "next
            # bar" is the full completed bucket -- booking mid-week would give a
            # weekly signal a 1-2 day fill window and (pre-dedup) re-book it daily.
            prior_frames = {tf: f.iloc[:-1] for tf, f in frames.items() if len(f) > 1}
            prior_signals = (analyze_frames(entry.ticker, prior_frames, cfg)
                             + analyze_reversals(entry.ticker, prior_frames, cfg))
            for pr in prior_signals:
                if not _is_period_end(today, pr.timeframe):
                    continue
                last = frames[pr.timeframe].iloc[-1]
                trig = prior_frames[pr.timeframe].index[-1].to_pydatetime()
                prior.append((pr, float(last["high"]), float(last["low"]), trig))
            # Re-screen the SAME prior frames under each alt screen variant (own fills).
            for vname, vcfg in alt_variants.items():
                vsignals = (analyze_frames(entry.ticker, prior_frames, vcfg)
                            + analyze_reversals(entry.ticker, prior_frames, vcfg))
                for pr in vsignals:
                    if not _is_period_end(today, pr.timeframe):
                        continue
                    last = frames[pr.timeframe].iloc[-1]
                    trig = prior_frames[pr.timeframe].index[-1].to_pydatetime()
                    prior_variants[vname].append(
                        (pr, float(last["high"]), float(last["low"]), trig))
        except Exception:  # per-ticker isolation: one bad ticker never aborts the run
            log.warning("ticker %s failed; skipping", entry.ticker, exc_info=True)
            n_failed += 1
            continue

    today_results.sort(key=lambda r: r.score, reverse=True)
    today_reversals.sort(key=lambda r: r.score, reverse=True)

    # Market regime once per run (SPY proxy), stamped onto every fill for WHEN-it-works
    # attribution. Skip the fetch entirely when there's nothing to fill (e.g. an empty
    # universe) -- no fills, no tags. Routed through the same fetch seam as the universe so
    # tests stay offline; SPY unavailable -> unknown regime (fails safe, never raises).
    spy_daily = None
    if prior or any(prior_variants.values()):
        spy_daily = _fetch_all_timeframes(
            MARKET_PROXY, cache_dir=cache_dir, today=today, cfg=cfg).get("1d")
    regime = classify_regime(spy_daily, cfg)

    n_charts = 0
    n_paper_opened = 0
    with Session(engine) as s:
        # Streak-start per (ticker, timeframe, play_type): inherit first_seen_date from
        # the prior run if the same setup fired then, else today. Read BEFORE deleting
        # today's rows (the lookup only considers run_date < today, so it's unaffected).
        prior_seen = repo.prior_first_seen(s, today)

        def _first_seen(r: SignalResult) -> date:
            return prior_seen.get((r.ticker, r.timeframe, r.play_type), today)

        repo.delete_signals_for(s, today)
        repo.delete_paper_trades_opened_on(s, today)
        # Rank WITHIN each play_type (independent top-N lists for the two sections).
        cont_signals = [_to_signal(r, rank, today, _first_seen(r))
                        for rank, r in enumerate(today_results, start=1)]
        rev_signals = [_to_signal(r, rank, today, _first_seen(r))
                       for rank, r in enumerate(today_reversals, start=1)]
        repo.save_signals(s, cont_signals + rev_signals)

        # Continuation: chart the global top-N (sector-capped, mirroring the daily digest)
        # plus each per-timeframe cadence's top-N. Reversal: a single daily top-N list.
        def _sector_of(r: SignalResult) -> str | None:
            v = universe_metrics.get(r.ticker, {}).get("sector")
            return v if isinstance(v, str) else None

        # The digest's staleness cooldown is deterministic at screen time (today's rows
        # are already saved, so the run-date window the digest will count over exists
        # right here) -- feed it to the chart selectors so tomorrow's fresh-promoted
        # picks are charted tonight instead of shipping as chartless PDF sections.
        cooldown = cfg.digest_repeat_cooldown_days
        window = max(cooldown or 0, *_TF_COOLDOWN_RUNS.values()) + 1
        recent_runs = repo.recent_run_dates(s, today, limit=window)
        for i in _digest_chart_indices(today_results, top_charts, sector_of=_sector_of,
                                       max_per_sector=cfg.daily_max_per_sector,
                                       first_seen_of=_first_seen,
                                       recent_run_dates=recent_runs,
                                       daily_cooldown_runs=cooldown):
            _render_and_attach(today_results[i], cont_signals[i], chart_dir, today)
            n_charts += 1
        # Reversal charts cover the digest's whole ELIGIBLE pool (confirmed-only to pool
        # depth, matching notify.select.reversal_picks + its backfill), not the raw
        # top-N by score, which EARLY signals dominate -- the emailed picks used to
        # arrive as chartless PDF sections (2026-07-03 diagnosis).
        for i in _reversal_chart_indices(today_reversals, cfg, top_n=top_charts,
                                         pool_n=REVERSAL_POOL_N,
                                         first_seen_of=_first_seen,
                                         recent_run_dates=recent_runs,
                                         daily_cooldown_runs=cooldown):
            _render_and_attach(today_reversals[i], rev_signals[i], chart_dir, today)
            n_charts += 1
        s.commit()

        # Parallel-arm shadow book: every fill is opened once per arm and advanced
        # under its own arm config, so breakdown(trades, "arm") is a same-sample A/B.
        arms = build_arms(cfg)
        candidates, next_bars = _shadow_candidates(prior, cfg)
        opened = open_from_signals(s, candidates, next_bars, fill_date=today,
                                   arms=tuple(arms), variant=DEFAULT_VARIANT,
                                   market_trend=regime.trend, market_vol=regime.vol,
                                   reversal_fill_window_bars=cfg.reversal_fill_window_bars)
        # count distinct fills (one arm), not the per-arm duplicates
        n_paper_opened = sum(1 for t in opened if t.status == "open" and t.arm == BASELINE)
        # Screen variants: book each alt config's own fills under the baseline exit only
        # (one extra book per variant), so breakdown(baseline-arm trades, "variant") ranks
        # the screen configs head-to-head. advance_open keys exits off `arm`, so these ride
        # the baseline exit automatically -- no variant awareness needed downstream.
        for vname, vcfg in alt_variants.items():
            vcands, vnext = _shadow_candidates(prior_variants[vname], vcfg)
            open_from_signals(s, vcands, vnext, fill_date=today, arms=(BASELINE,),
                              variant=vname, market_trend=regime.trend,
                              market_vol=regime.vol,
                              reversal_fill_window_bars=vcfg.reversal_fill_window_bars)
        # Step the PENDING resting-limit orders before the open trades: a pending order
        # that fills on this bar is then skipped by the stepper's entry-bar guard.
        # Per-variant windows: each variant's pending rows expire under its own config.
        resolve_pending(s, latest_bars, today=today,
                        window={n: c.reversal_fill_window_bars
                                for n, c in screen_variants.items()})
        advance_open(s, latest_bars, arms, today=today)
        # Live book: the bar-stepper above excludes account="live" rows -- the BROKER owns
        # their fills/exits. Reconcile them here (same cadence) so a broker fill materializes
        # a live position + a venue close reconciles its exit. Runs in live mode -- AND,
        # disarm-safety, whenever OPEN live exposure exists even after the mode is flipped
        # off: disarming used to stop the reconcile precisely when the operator was trying
        # to reduce risk, leaving the live book dark while positions sat at the venue
        # (2026-07 review). Broker construction for that path is guarded: missing broker
        # secrets must degrade to a loud warning, never kill the screen run.
        if broker is None and settings.broker and repo.load_open_live_trades(s):
            try:
                broker = build_broker(settings)
            except Exception:
                log.warning("open LIVE exposure exists but the broker could not be built; "
                            "live book NOT reconciled this run", exc_info=True)
        if broker is not None and (settings.execution_mode == "live"
                                   or repo.load_open_live_trades(s)):
            n_reconciled = reconcile_live(s, broker, today=today)
            log.info("live reconcile: %d change(s)", n_reconciled)

        # Enrich the universe rows with the metrics gathered during the loop
        # (one batch UPDATE; None-skips, self-commits).
        repo.apply_universe_metrics(s, universe_metrics)

    result = RunResult(n_signals=len(today_results) + len(today_reversals),
                       n_paper_opened=n_paper_opened, n_charts=n_charts, n_failed=n_failed,
                       n_reversals=len(today_reversals))
    # Stable ops marker: the Azure "missing evening screen" alert (infra/modules/
    # alerts.bicep) greps ContainerAppConsoleLogs_CL for the literal token
    # SCREEN_RUN_COMPLETE. Reword ONLY together with that KQL.
    log.info("SCREEN_RUN_COMPLETE signals=%d reversals=%d paper_opened=%d charts=%d failed=%d",
             result.n_signals, result.n_reversals, result.n_paper_opened, result.n_charts,
             result.n_failed)
    return result


def _resolve_db_url(cli_db: str | None) -> str:
    """Resolve the DB URL (explicit ``--db`` wins over ``load_settings()``) and
    fail fast on a dangerous local-sqlite-in-the-cloud misconfiguration.

    In a cloud context -- ``KEY_VAULT_URL`` set, or an explicit ``SWING_REQUIRE_DB``
    -- a sqlite URL almost certainly means SWING_DB_URL was never wired up, and
    silently screening into a throwaway local file that vanishes with the
    container is worse than crashing. So we refuse it.
    """
    db_url = cli_db if cli_db is not None else load_settings().db_url
    in_cloud = bool(os.environ.get("KEY_VAULT_URL") or os.environ.get("SWING_REQUIRE_DB"))
    if in_cloud and db_url.startswith("sqlite"):
        raise RuntimeError(
            "refusing to run against throwaway SQLite in a cloud context; "
            "set SWING_DB_URL to the Azure SQL URL"
        )
    return db_url


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the swing screener nightly pipeline.")
    # defaults come from load_settings() (absolute, env-first) so a container
    # picks up SWING_DB_URL/dirs; a bare local run still points at local.db/.cache.
    settings = load_settings()
    parser.add_argument("--db", default=None)
    parser.add_argument("--universe", type=Path,
                        default=Path("src/swing_screener/data/universe_seed.csv"))
    parser.add_argument("--cache-dir", type=Path, default=settings.cache_dir)
    parser.add_argument("--chart-dir", type=Path, default=settings.chart_dir)
    parser.add_argument("--top-charts", type=int, default=3)
    parser.add_argument("--max-tickers", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    db_url = _resolve_db_url(args.db)
    result = run_screen(universe_path=args.universe, db_url=db_url, cache_dir=args.cache_dir,
                        chart_dir=args.chart_dir, top_charts=args.top_charts,
                        max_tickers=args.max_tickers)
    log.info("signals=%d reversals=%d paper_opened=%d charts=%d failed=%d",
             result.n_signals, result.n_reversals, result.n_paper_opened,
             result.n_charts, result.n_failed)


if __name__ == "__main__":
    main()

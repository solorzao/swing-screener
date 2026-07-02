import argparse
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
from swing_screener.pipeline.analyze import (
    SignalResult,
    analyze_frames,
    analyze_reversals,
    build_frames,
)
from swing_screener.pipeline.arms import BASELINE, build_arms
from swing_screener.pipeline.diversity import cap_by_sector
from swing_screener.pipeline.broker import BrokerClient
from swing_screener.pipeline.broker_alpaca import build_broker
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
    from alembic import command
    from alembic.config import Config

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
        except Exception as exc:  # noqa: BLE001 -- inspect message, decide retry
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


def _digest_chart_indices(
    results: list[SignalResult], top_n: int, *,
    sector_of: "Callable[[SignalResult], str | None] | None" = None,
    max_per_sector: int | None = None,
) -> list[int]:
    """Indices into score-sorted ``results`` for every signal a digest can pick.

    Charts are rendered for the UNION of the global top-N (the daily digest) and
    the top-N within each per-timeframe cadence (weekly=1wk, monthly=1mo). Without
    the per-timeframe slices a weekly/monthly pick ranked below the global top-N
    would reach the digest with no chart. ``results`` is sorted by score
    descending, so a timeframe's first ``top_n`` entries are exactly its picks.
    Kept in sync with notify.select, whose pickers all default to top_n=5.

    When ``max_per_sector``/``sector_of`` are given the daily slice mirrors
    notify.select.daily_picks' sector cap, so a pick promoted into the daily list by
    the cap is charted (and one capped OUT isn't needlessly rendered).
    """
    if max_per_sector is not None and sector_of is not None:
        capped = cap_by_sector(list(enumerate(results)), lambda p: sector_of(p[1]),
                               max_per_sector=max_per_sector, limit=top_n)
        idx = {i for i, _ in capped}
    else:
        idx = set(range(min(top_n, len(results))))  # global top-N (daily digest)
    for tf in _DIGEST_TIMEFRAMES:
        tf_indices = [i for i, r in enumerate(results) if r.timeframe == tf]
        idx.update(tf_indices[:top_n])
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


def _shadow_candidates(
    prior_list: list[tuple[SignalResult, float, float, datetime | None]],
) -> tuple[list[FillCandidate], dict[tuple[str, str], tuple[float, float]]]:
    """Build the shadow-book fill candidates + next-bar map for one variant's prior signals.

    Ranks WITHIN each play_type (continuation and reversal each rank from 1) by score --
    a ranking space distinct from the persisted ``Signal.rank``. The next-bar high/low is
    the actual traded bar, so it's config-independent across variants. Each candidate
    carries its trigger bar's timestamp for the cross-run booking dedup.
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
                      conviction_tier=pr.conviction_tier, trigger_ts=trig)
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
               top_charts: int = 5, cfg: StrategyConfig | None = None,
               today: date | None = None, max_tickers: int | None = None,
               migrate_fn: Callable[[str], None] | None = None,
               broker: BrokerClient | None = None) -> RunResult:
    cfg = cfg or StrategyConfig()
    today = today or date.today()
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

        for i in _digest_chart_indices(today_results, top_charts, sector_of=_sector_of,
                                       max_per_sector=cfg.daily_max_per_sector):
            _render_and_attach(today_results[i], cont_signals[i], chart_dir, today)
            n_charts += 1
        for i in range(min(top_charts, len(today_reversals))):
            _render_and_attach(today_reversals[i], rev_signals[i], chart_dir, today)
            n_charts += 1
        s.commit()

        # Parallel-arm shadow book: every fill is opened once per arm and advanced
        # under its own arm config, so breakdown(trades, "arm") is a same-sample A/B.
        arms = build_arms(cfg)
        candidates, next_bars = _shadow_candidates(prior)
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
            vcands, vnext = _shadow_candidates(prior_variants[vname])
            open_from_signals(s, vcands, vnext, fill_date=today, arms=(BASELINE,),
                              variant=vname, market_trend=regime.trend,
                              market_vol=regime.vol,
                              reversal_fill_window_bars=vcfg.reversal_fill_window_bars)
        # Step the PENDING resting-limit orders before the open trades: a pending order
        # that fills on this bar is then skipped by the stepper's entry-bar guard.
        resolve_pending(s, latest_bars, window=cfg.reversal_fill_window_bars, today=today)
        advance_open(s, latest_bars, arms, today=today)
        # Live book: the bar-stepper above excludes account="live" rows -- the BROKER owns
        # their fills/exits. Reconcile them here (same cadence) so a broker fill materializes
        # a live position + a venue close reconciles its exit. Only when live + a broker; the
        # off/paper path never reaches here (broker is None).
        if broker is not None and settings.execution_mode == "live":
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
    parser.add_argument("--top-charts", type=int, default=5)
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

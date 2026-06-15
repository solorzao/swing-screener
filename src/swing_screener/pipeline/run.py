import argparse
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from swing_screener.charts.render import render_chart
from swing_screener.config import StrategyConfig
from swing_screener.data.fetch import fetch_bars
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
from swing_screener.pipeline.shadow import FillCandidate, advance_open, open_from_signals
from swing_screener.settings import load_settings
from swing_screener.storage.blob import blob_enabled, upload_chart

log = logging.getLogger(__name__)

_BAR_KEYS = ("low", "high", "close", "shaved_head", "bearish")

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


def _digest_chart_indices(results: list[SignalResult], top_n: int) -> list[int]:
    """Indices into score-sorted ``results`` for every signal a digest can pick.

    Charts are rendered for the UNION of the global top-N (the daily digest) and
    the top-N within each per-timeframe cadence (weekly=1wk, monthly=1mo). Without
    the per-timeframe slices a weekly/monthly pick ranked below the global top-N
    would reach the digest with no chart. ``results`` is sorted by score
    descending, so a timeframe's first ``top_n`` entries are exactly its picks.
    Kept in sync with notify.select, whose pickers all default to top_n=5.
    """
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
    return {k: last[k] for k in _BAR_KEYS}


def _to_signal(r: SignalResult, rank: int, run_date: date) -> Signal:
    return Signal(
        run_date=run_date, ticker=r.ticker, timeframe=r.timeframe, horizon=r.horizon,
        play_type=r.play_type, strength=r.strength,
        score=r.score, rank=rank, mtf_aligned=r.mtf_aligned, quality_tier=r.quality_tier,
        volatility_tier=r.volatility_tier, oversold=r.oversold, trigger_close=r.trigger_close,
        atr=r.atr, rsi=r.rsi, entry_floor=r.entry_floor, entry_ceiling=r.entry_ceiling,
        stop=r.stop, target=r.target,
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
               migrate_fn: Callable[[str], None] | None = None) -> RunResult:
    cfg = cfg or StrategyConfig()
    today = today or date.today()

    # Azure SQL: Alembic owns the schema, so upgrade to head BEFORE any engine
    # use (sqlite still goes through get_engine, which create_all's). Done up
    # front so a misconfigured/asleep DB fails before we spend time fetching.
    if db_url.startswith("mssql"):
        (migrate_fn or _migrate_with_retry)(db_url)

    universe = load_universe(universe_path)
    if max_tickers is not None:
        universe = universe[:max_tickers]
    engine = get_engine(db_url)

    today_results: list[SignalResult] = []      # continuation
    today_reversals: list[SignalResult] = []     # reversal
    prior: list[tuple[SignalResult, float, float]] = []  # (prior signal, next_high, next_low)
    latest_bars: dict[tuple[str, str], dict[str, float | bool]] = {}

    n_failed = 0
    for entry in universe:
        try:
            bars_by_tf = _fetch_all_timeframes(entry.ticker, cache_dir=cache_dir,
                                               today=today, cfg=cfg)
            if not bars_by_tf:
                continue
            frames = build_frames(bars_by_tf, cfg)
            for tf, f in frames.items():
                if len(f):
                    latest_bars[(entry.ticker, tf)] = _bar_row(f)
            today_results.extend(analyze_frames(entry.ticker, frames, cfg))
            today_reversals.extend(analyze_reversals(entry.ticker, frames, cfg))
            # Prior-bar signals (both play types) feed the shadow book: a signal that
            # fired on the prior bar is filled if today's bar trades into its zone.
            prior_frames = {tf: f.iloc[:-1] for tf, f in frames.items() if len(f) > 1}
            prior_signals = (analyze_frames(entry.ticker, prior_frames, cfg)
                             + analyze_reversals(entry.ticker, prior_frames, cfg))
            for pr in prior_signals:
                last = frames[pr.timeframe].iloc[-1]
                prior.append((pr, float(last["high"]), float(last["low"])))
        except Exception:  # per-ticker isolation: one bad ticker never aborts the run
            log.warning("ticker %s failed; skipping", entry.ticker, exc_info=True)
            n_failed += 1
            continue

    today_results.sort(key=lambda r: r.score, reverse=True)
    today_reversals.sort(key=lambda r: r.score, reverse=True)

    n_charts = 0
    n_paper_opened = 0
    with Session(engine) as s:
        repo.delete_signals_for(s, today)
        repo.delete_paper_trades_opened_on(s, today)
        # Rank WITHIN each play_type (independent top-N lists for the two sections).
        cont_signals = [_to_signal(r, rank, today)
                        for rank, r in enumerate(today_results, start=1)]
        rev_signals = [_to_signal(r, rank, today)
                       for rank, r in enumerate(today_reversals, start=1)]
        repo.save_signals(s, cont_signals + rev_signals)

        # Continuation: chart the global top-N plus each per-timeframe cadence's top-N
        # (daily/weekly/monthly). Reversal: a single daily top-N list, so chart its top-N.
        for i in _digest_chart_indices(today_results, top_charts):
            _render_and_attach(today_results[i], cont_signals[i], chart_dir, today)
            n_charts += 1
        for i in range(min(top_charts, len(today_reversals))):
            _render_and_attach(today_reversals[i], rev_signals[i], chart_dir, today)
            n_charts += 1
        s.commit()

        # NOTE: `rank` here is the rank within the prior-bar (forward-tested) set
        # being filled this run -- a different ranking space from Signal.rank. Ranked
        # within play_type so continuation and reversal shadow trades each rank from 1.
        prior_cont = sorted((x for x in prior if x[0].play_type == "continuation"),
                            key=lambda x: x[0].score, reverse=True)
        prior_rev = sorted((x for x in prior if x[0].play_type == "reversal"),
                           key=lambda x: x[0].score, reverse=True)
        candidates = [
            FillCandidate(pr.ticker, pr.timeframe, pr.horizon, pr.score, rank,
                          pr.mtf_aligned, None, pr.zone, quality_tier=pr.quality_tier,
                          volatility_tier=pr.volatility_tier, oversold=pr.oversold,
                          play_type=pr.play_type, strength=pr.strength)
            for group in (prior_cont, prior_rev)
            for rank, (pr, _h, _l) in enumerate(group, start=1)
        ]
        next_bars = {(pr.ticker, pr.timeframe): (h, low) for (pr, h, low) in prior}
        opened = open_from_signals(s, candidates, next_bars, fill_date=today)
        n_paper_opened = sum(1 for t in opened if t.status == "open")
        advance_open(s, latest_bars, cfg, today=today)

    return RunResult(n_signals=len(today_results) + len(today_reversals),
                     n_paper_opened=n_paper_opened, n_charts=n_charts, n_failed=n_failed,
                     n_reversals=len(today_reversals))


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
    log.info("signals=%d paper_opened=%d charts=%d failed=%d",
             result.n_signals, result.n_paper_opened, result.n_charts, result.n_failed)


if __name__ == "__main__":
    main()

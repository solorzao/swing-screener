"""Offline replay harness: walk the screener forward over historical bars and rank
screen variants on a leaderboard -- the offline complement to the live shadow book.

No network. Feed it OHLCV frames you already have (the parquet cache or a CSV fixture);
it drives the SAME shadow book (``open_from_signals`` + ``advance_open``) bar-by-bar
against a throwaway SQLite db, so the exit machinery (partials, trails) is reproduced
EXACTLY rather than re-implemented, then reads back ``breakdown(trades, "variant")``.

Walks one decision per bar, so it assumes one bar per calendar day -- valid for the
daily/weekly/monthly timeframes (distinct dates). 4h bars share a date and would trip
``advance_open``'s same-day guard, so they need per-day batching (not yet supported).
"""

import argparse
import logging
import tempfile
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import (
    PerformanceSummary,
    breakdown,
    leaderboard_flag,
    leaderboard_order,
)
from swing_screener.config import StrategyConfig
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.analyze import analyze_frames, analyze_reversals
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.regime import MarketRegime, classify_regime
from swing_screener.pipeline.run import _bar_row, _shadow_candidates
from swing_screener.pipeline.shadow import advance_open, open_from_signals
from swing_screener.pipeline.variants import build_screen_variants
from swing_screener.signals.frame import build_frame

log = logging.getLogger(__name__)


def _warmup(cfg: StrategyConfig) -> int:
    """Bars to skip before detection can fire (matches ``detect_last_bar``'s guard)."""
    return cfg.ema_slow + cfg.max_pullback_bars + 5


def _regime_by_date(spy_daily: pd.DataFrame, cfg: StrategyConfig) -> dict[date, MarketRegime]:
    """Point-in-time SPY regime as-of each date: classify_regime over spy_daily.iloc[:i+1]
    for each i (uses only bars <= that date -- no lookahead). Caller treats absent dates as
    unknown (None/None)."""
    out: dict[date, MarketRegime] = {}
    for i in range(len(spy_daily)):
        out[spy_daily.index[i].date()] = classify_regime(spy_daily.iloc[: i + 1], cfg)
    return out


def replay_book(
    frames: Mapping[str, pd.DataFrame],
    *,
    timeframe: str,
    base_cfg: StrategyConfig | None = None,
    variants: Mapping[str, StrategyConfig] | None = None,
    spy_daily: pd.DataFrame | None = None,
) -> list[PaperTrade]:
    """Walk each ticker forward and return the raw shadow-book PaperTrades (detached).

    The trade-level substrate the leaderboard, no-lookahead test, and propose() delta
    test all read. Behavior-identical to the loop previously inlined in ``replay()``.

    ``frames`` maps ticker -> raw OHLCV (the harness enriches once with ``base_cfg``;
    variants must share its indicator periods). ``variants`` defaults to
    ``build_screen_variants(base_cfg)``. When ``spy_daily`` is given, each fill is stamped
    with the point-in-time SPY regime as-of its fill date (no lookahead -- see
    ``_regime_by_date``); omit it and the regime fields stay ``None`` (back-compat). The
    returned trades are expunged from the throwaway session so callers can read their
    columns after it is torn down.
    """
    base_cfg = base_cfg or StrategyConfig()
    variants = variants or build_screen_variants(base_cfg)
    warmup = _warmup(base_cfg)
    regime_by_date = _regime_by_date(spy_daily, base_cfg) if spy_daily is not None else {}

    # One throwaway file db for the whole replay (in-memory sqlite would not survive the
    # per-bar session churn); a single Session streams every write through one connection.
    with tempfile.TemporaryDirectory() as tmp:
        engine = get_engine(f"sqlite:///{Path(tmp) / 'replay.db'}")
        try:
            with Session(engine) as s:
                for ticker, raw in frames.items():
                    if raw is None or len(raw) <= warmup:
                        continue
                    enriched = build_frame(raw, base_cfg)
                    _replay_one(s, ticker, timeframe, enriched, base_cfg, variants, warmup,
                                regime_by_date)
                trades = list(s.scalars(select(PaperTrade)))
                for t in trades:
                    s.expunge(t)
        finally:
            # Release the pooled connection so SQLite drops its file handle before the
            # TemporaryDirectory is torn down (Windows refuses to unlink an open file).
            engine.dispose()
        return trades


def replay(
    frames: Mapping[str, pd.DataFrame],
    *,
    timeframe: str,
    base_cfg: StrategyConfig | None = None,
    variants: Mapping[str, StrategyConfig] | None = None,
    spy_daily: pd.DataFrame | None = None,
) -> dict[str, PerformanceSummary]:
    """Walk each ticker's raw OHLCV frame forward and rank the screen variants.

    ``frames`` maps ticker -> raw OHLCV (the harness enriches once with ``base_cfg``;
    variants must share its indicator periods). ``variants`` defaults to
    ``build_screen_variants(base_cfg)``. Pass ``spy_daily`` to opt into point-in-time
    SPY-regime stamping (forwarded to ``replay_book``). Returns
    ``{variant: PerformanceSummary}`` over the pooled fills, each variant booked under the
    baseline exit. Thin wrapper over ``replay_book``.
    """
    return breakdown(
        replay_book(frames, timeframe=timeframe, base_cfg=base_cfg, variants=variants,
                    spy_daily=spy_daily),
        "variant",
    )


def _replay_one(
    session: Session,
    ticker: str,
    timeframe: str,
    enriched: pd.DataFrame,
    base_cfg: StrategyConfig,
    variants: Mapping[str, StrategyConfig],
    warmup: int,
    regime_by_date: Mapping[date, MarketRegime] = {},
) -> None:
    """Walk one enriched frame forward, booking each variant's fills under baseline exit.

    ``regime_by_date`` maps a fill date to its point-in-time SPY regime; absent dates (and
    the empty default) stamp the trade with an unknown regime (None/None).
    """
    for i in range(warmup, len(enriched)):
        through = enriched.iloc[: i + 1]            # data known at decision time i
        prior = through.iloc[:-1]                   # the trigger bar is i-1
        bar = through.iloc[-1]                      # the fill/advance bar is i
        fill_date = through.index[-1].date()
        bar_hl = (float(bar["high"]), float(bar["low"]))
        reg = regime_by_date.get(fill_date)

        for vname, vcfg in variants.items():
            sigs = (analyze_frames(ticker, {timeframe: prior}, vcfg)
                    + analyze_reversals(ticker, {timeframe: prior}, vcfg))
            if not sigs:
                continue
            prior_list = [(sig, *bar_hl) for sig in sigs]
            cands, next_bars = _shadow_candidates(prior_list)
            open_from_signals(session, cands, next_bars, fill_date=fill_date,
                              arms=(BASELINE,), variant=vname,
                              market_trend=reg.trend if reg else None,
                              market_vol=reg.vol if reg else None)

        # Advance every open trade one bar under the baseline exit (arm-keyed downstream).
        latest = {(ticker, timeframe): _bar_row(through)}
        advance_open(session, latest, {BASELINE: base_cfg}, today=fill_date)


def format_leaderboard(by_variant: Mapping[str, PerformanceSummary]) -> str:
    """A fixed-width leaderboard table, most-trustworthy first.

    Ranks trusted samples (>= ``MIN_LEADERBOARD_N`` closed) above thin ones, then by the
    lower 95% bound of expectancy -- matching the dashboard, so a thin lucky variant can't
    top a deeper one. The sample size + interval are printed so the ranking is auditable.
    """
    order = leaderboard_order(by_variant)
    header = (f"{'variant':<18}{'expectancy_r':>14}{'95%_low':>10}"
              f"{'win_rate':>10}{'closed':>8}{'clusters':>10}{'sample':>8}")
    lines = [header, "-" * len(header)]
    for v in order:
        s = by_variant[v]
        flag = leaderboard_flag(s)   # shared with the dashboard (iid > thin > ok)
        lines.append(f"{v:<18}{s.expectancy_r:>14.2f}{s.expectancy_ci_low:>10.2f}"
                     f"{s.win_rate:>10.2f}{s.n_closed:>8d}{s.n_clusters:>10d}{flag:>8}")
    return "\n".join(lines)


def _load_cached_daily(ticker: str, cache_dir: Path) -> pd.DataFrame | None:
    """Most recent cached daily parquet for a ticker (``<cache>/1d/<T>_<date>.parquet``)."""
    files = sorted((cache_dir / "1d").glob(f"{ticker}_*.parquet"))
    if not files:
        return None
    return pd.read_parquet(files[-1])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay the screener over cached daily history and rank screen variants.")
    parser.add_argument("--tickers", required=True, help="comma-separated, e.g. AMD,NVDA")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    frames: dict[str, pd.DataFrame] = {}
    for ticker in (t.strip().upper() for t in args.tickers.split(",") if t.strip()):
        df = _load_cached_daily(ticker, args.cache_dir)
        if df is None:
            log.warning("no cached daily data for %s; skipping", ticker)
            continue
        frames[ticker] = df

    if not frames:
        log.error("no data to replay (looked in %s/1d)", args.cache_dir)
        return
    board = replay(frames, timeframe="1d")
    print(format_leaderboard(board))  # noqa: T201 -- CLI output is the point


if __name__ == "__main__":
    main()

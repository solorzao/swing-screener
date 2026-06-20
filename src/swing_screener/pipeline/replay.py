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
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import PerformanceSummary, breakdown
from swing_screener.config import StrategyConfig
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.analyze import analyze_frames, analyze_reversals
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.run import _bar_row, _shadow_candidates
from swing_screener.pipeline.shadow import advance_open, open_from_signals
from swing_screener.pipeline.variants import build_screen_variants
from swing_screener.signals.frame import build_frame

log = logging.getLogger(__name__)


def _warmup(cfg: StrategyConfig) -> int:
    """Bars to skip before detection can fire (matches ``detect_last_bar``'s guard)."""
    return cfg.ema_slow + cfg.max_pullback_bars + 5


def replay(
    frames: Mapping[str, pd.DataFrame],
    *,
    timeframe: str,
    base_cfg: StrategyConfig | None = None,
    variants: Mapping[str, StrategyConfig] | None = None,
) -> dict[str, PerformanceSummary]:
    """Walk each ticker's raw OHLCV frame forward and rank the screen variants.

    ``frames`` maps ticker -> raw OHLCV (the harness enriches once with ``base_cfg``;
    variants must share its indicator periods). ``variants`` defaults to
    ``build_screen_variants(base_cfg)``. Returns ``{variant: PerformanceSummary}`` over
    the pooled fills, each variant booked under the baseline exit.
    """
    base_cfg = base_cfg or StrategyConfig()
    variants = variants or build_screen_variants(base_cfg)
    warmup = _warmup(base_cfg)

    # One throwaway file db for the whole replay (in-memory sqlite would not survive the
    # per-bar session churn); a single Session streams every write through one connection.
    with tempfile.TemporaryDirectory() as tmp:
        engine = get_engine(f"sqlite:///{Path(tmp) / 'replay.db'}")
        with Session(engine) as s:
            for ticker, raw in frames.items():
                if raw is None or len(raw) <= warmup:
                    continue
                enriched = build_frame(raw, base_cfg)
                _replay_one(s, ticker, timeframe, enriched, base_cfg, variants, warmup)
            trades = list(s.scalars(select(PaperTrade)))
    return breakdown(trades, "variant")


def _replay_one(
    session: Session,
    ticker: str,
    timeframe: str,
    enriched: pd.DataFrame,
    base_cfg: StrategyConfig,
    variants: Mapping[str, StrategyConfig],
    warmup: int,
) -> None:
    """Walk one enriched frame forward, booking each variant's fills under baseline exit."""
    for i in range(warmup, len(enriched)):
        through = enriched.iloc[: i + 1]            # data known at decision time i
        prior = through.iloc[:-1]                   # the trigger bar is i-1
        bar = through.iloc[-1]                      # the fill/advance bar is i
        fill_date = through.index[-1].date()
        bar_hl = (float(bar["high"]), float(bar["low"]))

        for vname, vcfg in variants.items():
            sigs = (analyze_frames(ticker, {timeframe: prior}, vcfg)
                    + analyze_reversals(ticker, {timeframe: prior}, vcfg))
            if not sigs:
                continue
            prior_list = [(sig, *bar_hl) for sig in sigs]
            cands, next_bars = _shadow_candidates(prior_list)
            open_from_signals(session, cands, next_bars, fill_date=fill_date,
                              arms=(BASELINE,), variant=vname)

        # Advance every open trade one bar under the baseline exit (arm-keyed downstream).
        latest = {(ticker, timeframe): _bar_row(through)}
        advance_open(session, latest, {BASELINE: base_cfg}, today=fill_date)


def format_leaderboard(by_variant: Mapping[str, PerformanceSummary]) -> str:
    """A fixed-width leaderboard table, best expectancy first."""
    order = sorted(by_variant, key=lambda v: by_variant[v].expectancy_r, reverse=True)
    header = f"{'variant':<18}{'expectancy_r':>14}{'win_rate':>10}{'fill_rate':>11}{'closed':>8}"
    lines = [header, "-" * len(header)]
    for v in order:
        s = by_variant[v]
        lines.append(f"{v:<18}{s.expectancy_r:>14.2f}{s.win_rate:>10.2f}"
                     f"{s.fill_rate:>11.2f}{s.n_closed:>8d}")
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

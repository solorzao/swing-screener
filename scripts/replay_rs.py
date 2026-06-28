"""Validate the RS-vs-SPY leadership gate on the reversal book (edge-discovery wave 2).

Threads SPY daily into replay_book (adds the rs column) and races the reversal book with vs
without require_rs_leader. Reversal-only + CONFIRMED-only breakdowns; optional slippage.

    python scripts/replay_rs.py [--limit 250] [--slippage 0.0]
"""

import argparse
import logging
from dataclasses import replace
from pathlib import Path

import pandas as pd

from swing_screener.analytics.performance import breakdown
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import _load_cached_daily, format_leaderboard, replay_book

log = logging.getLogger(__name__)


def _unique_tickers(cache_dir: Path) -> list[str]:
    return sorted({p.name.split("_")[0] for p in (cache_dir / "1d").glob("*.parquet")}
                  - {"^VIX", "SPY"})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    ap.add_argument("--limit", type=int, default=250)
    ap.add_argument("--slippage", type=float, default=0.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    tickers = _unique_tickers(args.cache_dir)
    if args.limit:
        tickers = tickers[: args.limit]
    frames: dict[str, pd.DataFrame] = {}
    for t in tickers:
        df = _load_cached_daily(t, args.cache_dir)
        if df is not None and len(df) > 60:
            frames[t] = df
    spy = _load_cached_daily("SPY", args.cache_dir)
    if spy is None:
        log.error("no SPY in cache")
        return
    log.info("RS validation: %d tickers (spy-threaded), slip=%.2f ...", len(frames), args.slippage)

    base = StrategyConfig(fill_slippage_atr=args.slippage)
    variants = {"default": base, "rs_leader": replace(base, require_rs_leader=True)}
    trades = replay_book(frames, timeframe="1d", base_cfg=base, variants=variants, spy_daily=spy)

    rev = [t for t in trades if t.arm == "baseline" and t.play_type == "reversal"]
    print("\n[reversal: rs_leader vs default]\n" + format_leaderboard(breakdown(rev, "variant")))  # noqa: T201
    conf = [t for t in rev if t.strength == "confirmed"]
    print("\n[reversal CONFIRMED-only]\n" + format_leaderboard(breakdown(conf, "variant")))  # noqa: T201


if __name__ == "__main__":
    main()

"""Race the Wyckoff spring trigger on the reversal book (edge-discovery exp 11).

default vs require_spring. Reports trade counts too -- if the spring barely prunes, it is the
no-op predicted (our reversal flip already opens near the bottom and reclaims).

    python scripts/replay_spring.py [--limit 250] [--slippage 0.0]
"""

import argparse
import logging
from dataclasses import replace
from pathlib import Path

import pandas as pd

from swing_screener.analytics.performance import breakdown
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import load_cached_daily, format_leaderboard, replay_book

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
        df = load_cached_daily(t, args.cache_dir)
        if df is not None and len(df) > 60:
            frames[t] = df
    log.info("spring race: %d tickers, slip=%.2f ...", len(frames), args.slippage)

    base = StrategyConfig(fill_slippage_atr=args.slippage)
    variants = {"default": base, "spring": replace(base, require_spring=True)}
    trades = replay_book(frames, timeframe="1d", base_cfg=base, variants=variants)
    rev = [t for t in trades if t.arm == "baseline" and t.play_type == "reversal"]
    print("\n[reversal: spring vs default]\n" + format_leaderboard(breakdown(rev, "variant")))  # noqa: T201


if __name__ == "__main__":
    main()

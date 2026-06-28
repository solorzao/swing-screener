"""Are the two reversal conviction filters additive or redundant? (promotion design input)

Races default vs rev_highvol (volume) vs spring (structure) vs both, reversal-only.

    python scripts/replay_rev_combo.py [--limit 250] [--slippage 0.0]
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
    log.info("rev combo: %d tickers, slip=%.2f ...", len(frames), args.slippage)

    base = StrategyConfig(fill_slippage_atr=args.slippage)
    variants = {
        "default": base,
        "highvol": replace(base, reversal_min_flip_rvol=1.3),
        "spring": replace(base, require_spring=True),
        "highvol_spring": replace(base, reversal_min_flip_rvol=1.3, require_spring=True),
    }
    trades = replay_book(frames, timeframe="1d", base_cfg=base, variants=variants)
    rev = [t for t in trades if t.arm == "baseline" and t.play_type == "reversal"]
    print("\n[reversal conviction filters: additive?]\n" + format_leaderboard(breakdown(rev, "variant")))  # noqa: T201


if __name__ == "__main__":
    main()

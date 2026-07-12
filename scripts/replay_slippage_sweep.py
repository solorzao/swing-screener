"""Costs/slippage sensitivity: does the reversal+no_flip edge survive a realistic fill
haircut? Runs the golden-master replay_book with momentum_flip_exit=False at several
fill_slippage_atr levels (a per-ATR haircut on LEVEL exits: stop/target; momentum_flip
and time_stop use the bar close and are NOT haircut). Breakdown by play_type.

    python scripts/replay_slippage_sweep.py [--limit N] [--slippage 0.0,0.05,0.10]
"""

import argparse
import logging
from pathlib import Path

import pandas as pd

from swing_screener.analytics.performance import breakdown
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import load_cached_daily, replay_book
from swing_screener.pipeline.variants import DEFAULT_VARIANT

log = logging.getLogger(__name__)


def _unique_tickers(cache_dir: Path) -> list[str]:
    return sorted({p.name.split("_")[0] for p in (cache_dir / "1d").glob("*.parquet")})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--limit", type=int, default=0, help="0 = all tickers")
    parser.add_argument("--slippage", default="0.0,0.05,0.10",
                        help="comma list of fill_slippage_atr levels to sweep")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    all_t = _unique_tickers(args.cache_dir)
    tickers = all_t[: args.limit] if args.limit else all_t
    frames: dict[str, pd.DataFrame] = {}
    for t in tickers:
        df = load_cached_daily(t, args.cache_dir)
        if df is not None and len(df) > 60:
            frames[t] = df
    levels = [float(x) for x in args.slippage.split(",") if x.strip()]
    log.info("sweeping %d tickers x %d slippage levels (reversal+no_flip) ...",
             len(frames), len(levels))

    hdr = (f"{'play_type':<14}{'slip_atr':>9}{'expectancy_r':>14}{'95%_low':>10}"
           f"{'win_rate':>10}{'closed':>8}{'clusters':>10}")
    rows: list[str] = []
    for s in levels:
        base = StrategyConfig(momentum_flip_exit=False, fill_slippage_atr=s)
        trades = [t for t in replay_book(frames, timeframe="1d", base_cfg=base,
                                         variants={DEFAULT_VARIANT: base})
                  if t.arm == "baseline"]
        by_play = breakdown(trades, "play_type")
        for pt in ("reversal", "continuation"):
            summ = by_play.get(pt)
            if summ is None:
                continue
            rows.append(f"{pt:<14}{s:>9.2f}{summ.expectancy_r:>14.3f}"
                        f"{summ.expectancy_ci_low:>10.3f}{summ.win_rate:>10.2f}"
                        f"{summ.n_closed:>8d}{summ.n_clusters:>10d}")
        log.info("  done slip=%.2f", s)

    print("\n" + hdr)        # noqa: T201
    print("-" * len(hdr))    # noqa: T201
    # group rows by play_type for readability
    for pt in ("reversal", "continuation"):
        for r in rows:
            if r.startswith(pt):
                print(r)     # noqa: T201


if __name__ == "__main__":
    main()

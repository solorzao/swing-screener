"""Continuation edge tournament round 3: cost-robustness of the best round-2 combos.

Sweeps fill_slippage_atr for the two strongest combos (vol_band, vol_band_body) over the
full basket via the golden-master replay_book, continuation-only. The bar (matching the
reversal edge): does the 95%low stay > 0 at ~0.05 ATR slippage?

Only 2 variants per walk so the full 503-name basket completes (6-variant x 503 gets killed).

    python scripts/replay_tournament3.py [--limit 0] [--slippage 0.0,0.05,0.10]
"""

import argparse
import logging
from dataclasses import replace
from pathlib import Path

import pandas as pd

from swing_screener.analytics.performance import breakdown
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import load_cached_daily, replay_book

log = logging.getLogger(__name__)


def variants_for(base: StrategyConfig) -> dict[str, StrategyConfig]:
    return {
        "vol_band": replace(base, vol_thrust_min=1.3, require_value_band=True),
        "vol_band_body": replace(base, vol_thrust_min=1.3, require_value_band=True,
                                 min_trigger_body_frac=0.5, max_trigger_lower_wick_frac=1.0),
    }


def _unique_tickers(cache_dir: Path) -> list[str]:
    return sorted({p.name.split("_")[0] for p in (cache_dir / "1d").glob("*.parquet")})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--limit", type=int, default=0, help="0 = all tickers")
    parser.add_argument("--slippage", default="0.0,0.05,0.10")
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
    log.info("round 3: %d tickers x 2 combos x %d slip levels ...", len(frames), len(levels))

    # Print incrementally (per slip level) and flush, so partial results survive if a long
    # 503-name run is killed mid-sweep (these multi-walk runs get killed partway).
    hdr = (f"{'variant':<14}{'slip_atr':>9}{'expectancy_r':>14}{'95%_low':>10}"
           f"{'win_rate':>10}{'closed':>8}{'clusters':>10}")
    print("\n" + hdr, flush=True)        # noqa: T201
    print("-" * len(hdr), flush=True)    # noqa: T201
    for s in levels:
        base = StrategyConfig(fill_slippage_atr=s)
        trades = [t for t in replay_book(frames, timeframe="1d", base_cfg=base,
                                         variants=variants_for(base))
                  if t.arm == "baseline" and t.play_type == "continuation"]
        by_variant = breakdown(trades, "variant")
        for v in ("vol_band", "vol_band_body"):
            summ = by_variant.get(v)
            if summ is None:
                continue
            print(f"{v:<14}{s:>9.2f}{summ.expectancy_r:>14.3f}"  # noqa: T201
                  f"{summ.expectancy_ci_low:>10.3f}{summ.win_rate:>10.2f}"
                  f"{summ.n_closed:>8d}{summ.n_clusters:>10d}", flush=True)
        log.info("  done slip=%.2f", s)


if __name__ == "__main__":
    main()

"""Continuation edge tournament round 2: combine the round-1 winners and test cost-robustness.

vol_thrust was the only positive round-1 lever (+0.10R); value_band and strong_body helped
modestly. This races threshold variants of vol_thrust and its combinations with value_band /
strong_body, continuation-only, via the golden-master replay_book. Pass --slippage to test
whether the best combo survives realistic fill costs (the bar: 95%low > 0 at ~0.05 ATR).

    python scripts/replay_tournament2.py [--limit 0] [--slippage 0.0]
"""

import argparse
import logging
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import pandas as pd

from swing_screener.analytics.performance import breakdown
from swing_screener.config import StrategyConfig
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.replay import _load_cached_daily, format_leaderboard, replay_book

log = logging.getLogger(__name__)


def build_round2_variants(base: StrategyConfig) -> dict[str, StrategyConfig]:
    return {
        "vol_thrust_13": replace(base, vol_thrust_min=1.3),
        "vol_thrust_15": replace(base, vol_thrust_min=1.5),
        "vol_thrust_20": replace(base, vol_thrust_min=2.0),
        "vol_band": replace(base, vol_thrust_min=1.3, require_value_band=True),
        "vol15_band": replace(base, vol_thrust_min=1.5, require_value_band=True),
        "vol_band_body": replace(base, vol_thrust_min=1.3, require_value_band=True,
                                 min_trigger_body_frac=0.5, max_trigger_lower_wick_frac=1.0),
    }


def _unique_tickers(cache_dir: Path) -> list[str]:
    return sorted({p.name.split("_")[0] for p in (cache_dir / "1d").glob("*.parquet")})


def _exit_mix(trades: list[PaperTrade]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if t.status == "closed" and t.realized_r is not None:
            out[t.exit_reason or "?"].append(float(t.realized_r))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--limit", type=int, default=0, help="0 = all tickers")
    parser.add_argument("--slippage", type=float, default=0.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    all_t = _unique_tickers(args.cache_dir)
    tickers = all_t[: args.limit] if args.limit else all_t
    frames: dict[str, pd.DataFrame] = {}
    for t in tickers:
        df = _load_cached_daily(t, args.cache_dir)
        if df is not None and len(df) > 60:
            frames[t] = df

    base = StrategyConfig(fill_slippage_atr=args.slippage)
    variants = build_round2_variants(base)
    log.info("round 2: %d tickers x %d combos (slip=%.2f), continuation-only ...",
             len(frames), len(variants), args.slippage)
    trades = replay_book(frames, timeframe="1d", base_cfg=base, variants=variants)

    cont = [t for t in trades if t.arm == "baseline" and t.play_type == "continuation"]
    print("\n[continuation-only leaderboard, slip=%.2f]\n" % args.slippage  # noqa: T201
          + format_leaderboard(breakdown(cont, "variant")))

    by_variant: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in cont:
        by_variant[t.variant].append(t)
    print("\nexit mix:")  # noqa: T201
    for vname in variants:
        mix = _exit_mix(by_variant.get(vname, []))
        total = sum(len(v) for v in mix.values())
        flip = mix.get("momentum_flip", [])
        tgt = mix.get("target", [])
        print(f"  {vname:<14} n={total:<5} "  # noqa: T201
              f"flip {(len(flip)/total if total else 0):5.1%}   "
              f"target {(len(tgt)/total if total else 0):5.1%} @ "
              f"{(sum(tgt)/len(tgt) if tgt else 0):+.2f}R")


if __name__ == "__main__":
    main()

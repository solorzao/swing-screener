"""Edge-discovery wave 1 races: volume dry-up + pocket pivot (continuation), and the
reversal flip-volume sign A/B. Golden-master replay_book, play_type-filtered.

    python scripts/replay_wave1.py --book cont [--limit 250] [--slippage 0.0]
    python scripts/replay_wave1.py --book rev  [--limit 250]
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
from swing_screener.pipeline.replay import load_cached_daily, format_leaderboard, replay_book

log = logging.getLogger(__name__)


def cont_variants(base):
    return {
        "default": base,
        "vol_thrust_13": replace(base, vol_thrust_min=1.3),
        "dryup": replace(base, pullback_vol_dryup_max=0.85),
        "pocket_pivot": replace(base, require_pocket_pivot=True),
        "volband_dryup": replace(base, vol_thrust_min=1.3, require_value_band=True,
                                 pullback_vol_dryup_max=0.85),
        "volband_pocket": replace(base, require_pocket_pivot=True, require_value_band=True),
    }


def rev_variants(base):
    return {
        "default": base,
        "rev_lowvol": replace(base, reversal_max_flip_rvol=1.0),
        "rev_highvol": replace(base, reversal_min_flip_rvol=1.3),
    }


def _unique_tickers(cache_dir: Path) -> list[str]:
    return sorted({p.name.split("_")[0] for p in (cache_dir / "1d").glob("*.parquet")})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--book", choices=["cont", "rev"], required=True)
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

    base = StrategyConfig(fill_slippage_atr=args.slippage)
    variants = cont_variants(base) if args.book == "cont" else rev_variants(base)
    play_type = "continuation" if args.book == "cont" else "reversal"
    log.info("wave1 %s: %d tickers x %d variants (slip=%.2f) ...",
             args.book, len(frames), len(variants), args.slippage)
    trades = replay_book(frames, timeframe="1d", base_cfg=base, variants=variants)

    sel = [t for t in trades if t.arm == "baseline" and t.play_type == play_type]
    print(f"\n[{play_type} leaderboard]\n" + format_leaderboard(breakdown(sel, "variant")))  # noqa: T201

    if args.book == "rev":  # the edge lives in the CONFIRMED subset -- show it too
        conf = [t for t in sel if t.strength == "confirmed"]
        print("\n[reversal CONFIRMED-only leaderboard]\n"  # noqa: T201
              + format_leaderboard(breakdown(conf, "variant")))

    print("\nmomentum_flip / target share by variant:")  # noqa: T201
    by_v: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in sel:
        by_v[t.variant].append(t)
    for vname in variants:
        g = [t for t in by_v.get(vname, []) if t.status == "closed" and t.realized_r is not None]
        n = len(g)
        flip = sum(1 for t in g if t.exit_reason == "momentum_flip")
        tgt = [t.realized_r for t in g if t.exit_reason == "target"]
        print(f"  {vname:<15} n={n:<5} flip {(flip/n if n else 0):5.1%}   "  # noqa: T201
              f"target {(len(tgt)/n if n else 0):5.1%} @ {(sum(tgt)/len(tgt) if tgt else 0):+.2f}R")


if __name__ == "__main__":
    main()

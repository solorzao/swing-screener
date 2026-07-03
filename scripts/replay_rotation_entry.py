"""Rotation-capture entry mechanics: can the reversal book buy a V-shaped bounce?

The 2026-07-01/02 software rotation was DETECTED (CRM/WDAY/INTU/PTC all fired) but
structurally untradeable: CONFIRMED signals are born with price above their own pullback
ceiling (the band anchors on the stale flip bar) and a V-move never retraces 38.2% to
fill the resting limit. This tournament races the legacy geometry against the three
default-off entry-mechanic knobs added for it:

  anchor_confirm  -- band anchored on the bounce top (reversal_anchor_confirmation)
  chase_close     -- ceiling at the trigger close (reversal_ceiling_at_close)
  confirm3        -- flip may confirm up to 3 bars late (reversal_confirm_window=3)
  ... and pairwise combos.

Walks run <=3 variants each (a 6-variant x 503 walk gets killed), reversal-only,
baseline arm, net of --slippage on exits, SPY regime + VIX stamped for cohort cuts.
Each walk's book is dumped to parquet for post-hoc attribution (strength, vol tier,
market vol, sector/breadth rotation days).

    python scripts/replay_rotation_entry.py [--limit 0] [--slippage 0.05] [--out DIR]
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

_DUMP_COLS = ("ticker", "variant", "play_type", "strength", "conviction_tier",
              "volatility_tier", "quality_tier", "fill_status", "status", "signal_score",
              "entry_date", "opened_date", "exit_date", "exit_reason", "realized_r",
              "hold_bars", "entry_price", "stop", "target", "risk",
              "market_trend", "market_vol", "vix_bucket")


def _unique_tickers(cache_dir: Path) -> list[str]:
    return sorted({p.name.split("_")[0] for p in (cache_dir / "1d").glob("*.parquet")}
                  - {"^VIX", "SPY"})


def walks_for(base: StrategyConfig) -> dict[str, dict[str, StrategyConfig]]:
    return {
        "A_geometry": {
            "default": base,
            "anchor_confirm": replace(base, reversal_anchor_confirmation=True),
            "chase_close": replace(base, reversal_ceiling_at_close=True),
        },
        "B_window": {
            "confirm3": replace(base, reversal_confirm_window=3),
            "confirm3_anchor": replace(base, reversal_confirm_window=3,
                                       reversal_anchor_confirmation=True),
            "chase_anchor": replace(base, reversal_ceiling_at_close=True,
                                    reversal_anchor_confirmation=True),
        },
        # cost-robustness pass for the round-1 winner: does confirm3 hold at 0.10 ATR
        # slippage, and is the settled high-volume filter additive on top of it?
        "C_robust": {
            "default": base,
            "confirm3": replace(base, reversal_confirm_window=3),
            "confirm3_highvol": replace(base, reversal_confirm_window=3,
                                        reversal_min_flip_rvol=1.3),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--slippage", type=float, default=0.05)
    ap.add_argument("--out", type=Path, default=Path(".cache/rotation_entry"))
    ap.add_argument("--walk", default=None, help="run only this walk (A_geometry/B_window)")
    ap.add_argument("--shard", default=None, metavar="I/N",
                    help="process only tickers[i::n] (a small per-shard book sidesteps the "
                         "accumulated-trades slowdown; merge the parquets afterwards)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    tickers = _unique_tickers(args.cache_dir)
    if args.limit:
        tickers = tickers[: args.limit]
    shard_tag = ""
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        tickers = tickers[i::n]
        shard_tag = f"_s{i}"
    frames: dict[str, pd.DataFrame] = {}
    for t in tickers:
        df = _load_cached_daily(t, args.cache_dir)
        if df is not None and len(df) > 60:
            frames[t] = df
    spy = _load_cached_daily("SPY", args.cache_dir)
    vix = _load_cached_daily("^VIX", args.cache_dir)
    log.info("rotation entry: %d tickers, slip=%.2f, spy=%s vix=%s",
             len(frames), args.slippage, spy is not None, vix is not None)

    base = StrategyConfig(fill_slippage_atr=args.slippage)
    args.out.mkdir(parents=True, exist_ok=True)

    for walk_name, variants in walks_for(base).items():
        if args.walk and walk_name != args.walk:
            continue
        log.info("=== walk %s: %s ===", walk_name, list(variants))
        trades = replay_book(frames, timeframe="1d", base_cfg=base, variants=variants,
                             spy_daily=spy, vix_daily=vix)
        rev = [t for t in trades if t.arm == "baseline" and t.play_type == "reversal"]
        pd.DataFrame([{c: getattr(t, c) for c in _DUMP_COLS} for t in rev]).to_parquet(
            args.out / f"{walk_name}{shard_tag}_slip{args.slippage:.2f}.parquet")
        log.info("walk %s%s done: %d reversal rows dumped", walk_name, shard_tag, len(rev))
        if shard_tag:  # shards only dump; leaderboards are computed on the merged book
            continue

        print(f"\n[{walk_name} | all reversals | slip={args.slippage}]")  # noqa: T201
        print(format_leaderboard(breakdown(rev, "variant")))  # noqa: T201
        for strength in ("confirmed", "early"):
            sub = [t for t in rev if t.strength == strength]
            print(f"\n[{walk_name} | {strength} only]")  # noqa: T201
            print(format_leaderboard(breakdown(sub, "variant")))  # noqa: T201


if __name__ == "__main__":
    main()

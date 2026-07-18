"""The 2026-07 queued-experiment walks, on a PINNED corpus.

  D_dump    -- the default book alone, BOTH play types dumped: the substrate for the
               continuation rank sweep and the mtf_aligned/timeframe cohort reads.
  R_target  -- reversal target geometry: reversal_retrace_frac {0.5, 0.618, 1.0} vs the
               shipped 0.786 (the exit side of the reversal book was never A/B'd; the
               knob flows through the booked trade, so the baseline-exit harness is
               sufficient).
  T_window  -- cont_confirm_window {1, 2}: the continuation analog of the reversal
               confirm window (fire on the first close above the flip high instead of
               the flip bar itself).
  V_thrust  -- the volume-thrust denominator A/B on the cont_volband combo (the legacy
               baseline window includes the pullback's own dried-up volume).

Each walk books <=3 variants (a 6-variant full-universe walk gets killed) and dumps its
baseline-arm trades (both play types) to parquet for post-hoc cuts. Shard for speed:

    python scripts/replay_queue_experiments.py --walk R_target --shard 0/12 \
        --as-of 20260703 --slippage 0.05
"""

import argparse
import logging
from dataclasses import replace
from pathlib import Path

import pandas as pd

from _replay_common import load_replay_corpus, unique_tickers
from swing_screener.analytics.performance import breakdown
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import (
    load_cached_daily,
    corpus_stamp,
    format_leaderboard,
    replay_book,
)

log = logging.getLogger(__name__)

_DUMP_COLS = ("ticker", "variant", "play_type", "strength", "conviction_tier",
              "volatility_tier", "quality_tier", "fill_status", "status", "signal_score",
              "rank", "mtf_aligned", "timeframe", "entry_date", "opened_date", "exit_date",
              "exit_reason", "realized_r", "hold_bars", "entry_price", "stop", "target",
              "risk", "low_water", "high_water", "market_trend", "market_vol", "vix_bucket")


def walks_for(base: StrategyConfig) -> dict[str, dict[str, StrategyConfig]]:
    return {
        "D_dump": {
            "default": base,
        },
        "R_target": {
            "retrace_050": replace(base, reversal_retrace_frac=0.5),
            "retrace_0618": replace(base, reversal_retrace_frac=0.618),
            "retrace_100": replace(base, reversal_retrace_frac=1.0),
        },
        "T_window": {
            "cont_confirm1": replace(base, cont_confirm_window=1),
            "cont_confirm2": replace(base, cont_confirm_window=2),
        },
        "V_thrust": {
            "volband_legacy": replace(base, vol_thrust_min=1.3, require_value_band=True,
                                      min_trigger_body_frac=0.5,
                                      max_trigger_lower_wick_frac=1.0),
            "volband_exclpb": replace(base, vol_thrust_min=1.3, require_value_band=True,
                                      min_trigger_body_frac=0.5,
                                      max_trigger_lower_wick_frac=1.0,
                                      vol_thrust_excl_pullback=True),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--slippage", type=float, default=0.05)
    ap.add_argument("--as-of", default=None, metavar="YYYYMMDD",
                    help="pin the corpus to cache snapshots at/before this fetch date")
    ap.add_argument("--out", type=Path, default=Path(".cache/queue_experiments"))
    ap.add_argument("--walk", required=True, help="D_dump / R_target / T_window / V_thrust")
    ap.add_argument("--shard", default=None, metavar="I/N", help="process tickers[i::n]")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    tickers = unique_tickers(args.cache_dir)
    if args.limit:
        tickers = tickers[: args.limit]
    shard_tag = ""
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        tickers = tickers[i::n]
        shard_tag = f"_s{i}"

    frames = load_replay_corpus(args.cache_dir, tickers=tickers, as_of=args.as_of)
    spy = load_cached_daily("SPY", args.cache_dir, args.as_of)
    vix = load_cached_daily("^VIX", args.cache_dir, args.as_of)
    if not shard_tag:
        log.info("%s", corpus_stamp(args.cache_dir, tickers, args.as_of))
    log.info("queue experiments %s: %d tickers, slip=%.2f", args.walk, len(frames),
             args.slippage)

    base = StrategyConfig(fill_slippage_atr=args.slippage)
    variants = walks_for(base)[args.walk]
    args.out.mkdir(parents=True, exist_ok=True)

    trades = replay_book(frames, timeframe="1d", base_cfg=base, variants=variants,
                         spy_daily=spy, vix_daily=vix)
    book = [t for t in trades if t.arm == "baseline"]
    pd.DataFrame([{c: getattr(t, c) for c in _DUMP_COLS} for t in book]).to_parquet(
        args.out / f"{args.walk}{shard_tag}_slip{args.slippage:.2f}.parquet")
    log.info("walk %s%s done: %d rows dumped", args.walk, shard_tag, len(book))
    if shard_tag:
        return
    for pt in ("continuation", "reversal"):
        sub = [t for t in book if t.play_type == pt]
        if sub:
            print(f"\n[{args.walk} | {pt} | slip={args.slippage}]")  # noqa: T201
            print(format_leaderboard(breakdown(sub, "variant")))  # noqa: T201


if __name__ == "__main__":
    main()

"""Throwaway: does disabling the eager momentum_flip exit help the REVERSAL book,
and does reversal beat continuation?

Runs the golden-master replay_book UNCHANGED twice over the same basket -- once with
momentum_flip_exit ON, once OFF -- and breaks each run down by play_type. Entries are
identical across the two runs (the exit policy doesn't affect detection/fills), so it's a
clean same-sample A/B on the exit. No hand-rolled walk (avoids the lookahead-class bug).

    python scripts/replay_reversal_flip.py [--limit N] [--cache-dir .cache]
"""

import argparse
import logging
from collections import defaultdict
from pathlib import Path

from _replay_common import load_replay_corpus
from swing_screener.analytics.performance import breakdown
from swing_screener.config import StrategyConfig
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.replay import replay_book
from swing_screener.pipeline.variants import DEFAULT_VARIANT

log = logging.getLogger(__name__)


def _exit_mix(trades: list[PaperTrade]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if t.status == "closed" and t.realized_r is not None:
            out[t.exit_reason or "?"].append(float(t.realized_r))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--limit", type=int, default=120, help="0 = all tickers")
    parser.add_argument("--policies", default="on,off",
                        help="comma list of exit policies to walk: on,off (default both)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    frames = load_replay_corpus(args.cache_dir, limit=args.limit)
    log.info("replaying %d tickers x 2 exit policies (flip on/off) ...", len(frames))

    # play_type -> flip_label -> (summary, trades)
    results: dict[str, dict[str, object]] = defaultdict(dict)
    mixes: dict[str, dict[str, dict[str, list[float]]]] = defaultdict(dict)
    want = {p.strip() for p in args.policies.split(",") if p.strip()}
    policies = [(lbl, flip) for lbl, flip in (("flip_on", True), ("flip_off", False))
                if lbl.split("_")[1] in want]
    for label, flip in policies:
        base = StrategyConfig(momentum_flip_exit=flip)
        trades = [t for t in replay_book(frames, timeframe="1d", base_cfg=base,
                                         variants={DEFAULT_VARIANT: base})
                  if t.arm == "baseline"]
        by_play = breakdown(trades, "play_type")
        by_play_trades: dict[str, list[PaperTrade]] = defaultdict(list)
        for t in trades:
            by_play_trades[t.play_type].append(t)
        for pt, summ in by_play.items():
            results[pt][label] = summ
            mixes[pt][label] = _exit_mix(by_play_trades[pt])

    hdr = (f"{'play_type':<14}{'exit':<10}{'expectancy_r':>14}{'95%_low':>10}"
           f"{'win_rate':>10}{'closed':>8}{'clusters':>10}")
    print("\n" + hdr)            # noqa: T201
    print("-" * len(hdr))        # noqa: T201
    for pt in sorted(results):
        for label in ("flip_on", "flip_off"):
            s = results[pt].get(label)
            if s is None:
                continue
            print(f"{pt:<14}{label:<10}{s.expectancy_r:>14.3f}{s.expectancy_ci_low:>10.3f}"  # noqa: T201
                  f"{s.win_rate:>10.2f}{s.n_closed:>8d}{s.n_clusters:>10d}")

    print("\nexit-reason mix:")  # noqa: T201
    for pt in sorted(mixes):
        for label in ("flip_on", "flip_off"):
            reasons = mixes[pt].get(label, {})
            total = sum(len(v) for v in reasons.values())
            print(f"\n  {pt} / {label}  (n_closed={total})")  # noqa: T201
            for reason, rs in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
                share = len(rs) / total if total else 0
                avg = sum(rs) / len(rs) if rs else 0
                print(f"    {reason:<14} {len(rs):>5}  ({share:5.1%})  avg {avg:+.2f}R")  # noqa: T201


if __name__ == "__main__":
    main()

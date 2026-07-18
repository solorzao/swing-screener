"""Validate item 1: the reversal conviction tier + conviction sizing.

Replays the reversal book (conviction_tier now stamped on every fill), breaks expectancy down
by tier (does premium really separate winners?), and compares plain vs size-weighted expectancy
(does sizing the edge cohort up lift the book?).

    python scripts/replay_sizing.py [--limit 250] [--slippage 0.05]
"""

import argparse
import logging
from pathlib import Path

from _replay_common import load_replay_corpus
from swing_screener.analytics.performance import breakdown, size_weighted_expectancy, summarize
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import replay_book

log = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    ap.add_argument("--limit", type=int, default=250)
    ap.add_argument("--slippage", type=float, default=0.05)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    frames = load_replay_corpus(args.cache_dir, limit=args.limit)
    base = StrategyConfig(fill_slippage_atr=args.slippage)
    log.info("sizing validation: %d tickers, slip=%.2f ...", len(frames), args.slippage)
    trades = replay_book(frames, timeframe="1d", base_cfg=base, variants={"default": base})
    rev = [t for t in trades if t.arm == "baseline" and t.play_type == "reversal"]

    print("\n[reversal expectancy by conviction tier]")  # noqa: T201
    hdr = f"{'tier':<10}{'expectancy_r':>14}{'95%_low':>10}{'win_rate':>10}{'closed':>8}{'clusters':>10}"
    print(hdr + "\n" + "-" * len(hdr))  # noqa: T201
    by_tier = breakdown(rev, "conviction_tier")
    for tier in ("premium", "strong", "base"):
        s = by_tier.get(tier)
        if s is None:
            continue
        print(f"{tier:<10}{s.expectancy_r:>14.3f}{s.expectancy_ci_low:>10.3f}"  # noqa: T201
              f"{s.win_rate:>10.2f}{s.n_closed:>8d}{s.n_clusters:>10d}")

    plain = summarize(rev).expectancy_r
    weighted, total_w = size_weighted_expectancy(rev)
    print(f"\nreversal book: plain expectancy {plain:+.3f}R  vs  "  # noqa: T201
          f"size-weighted {weighted:+.3f}R  (total weight {total_w:.0f})")


if __name__ == "__main__":
    main()

"""Validate the VIX-rank gate on the reversal CONFIRMED book (edge-discovery exp 15).

Races the confirmed reversal book with vs without max_vix_rank, and breaks the book down by
the stamped vix_bucket -- the direct test of whether high-VIX states are actually worse for
the oversold-bounce edge (and so whether the gate adds over the free market_vol stamp).

    python scripts/replay_vix.py [--limit 250] [--max-vix-rank 70]
"""

import argparse
import logging
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from _replay_common import load_replay_corpus
from swing_screener.analytics.performance import breakdown, summarize
from swing_screener.config import StrategyConfig
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.replay import load_cached_daily, format_leaderboard, replay_book

log = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    ap.add_argument("--limit", type=int, default=250)
    ap.add_argument("--max-vix-rank", type=float, default=70.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    frames = load_replay_corpus(args.cache_dir, limit=args.limit)
    vix = load_cached_daily("^VIX", args.cache_dir)
    if vix is None:
        log.error('no ^VIX in cache; warm it first: fetch_bars("^VIX", "1d", cache_dir=...)')
        return
    log.info("vix validation: %d tickers, gate max_vix_rank=%.0f ...", len(frames), args.max_vix_rank)

    base = StrategyConfig()
    variants = {"default": base, "vix_gated": replace(base, max_vix_rank=args.max_vix_rank)}
    trades = replay_book(frames, timeframe="1d", base_cfg=base, variants=variants, vix_daily=vix)

    # the confirmed reversal book is where the edge lives
    conf = [t for t in trades if t.arm == "baseline" and t.play_type == "reversal"
            and t.strength == "confirmed"]
    print("\n[reversal CONFIRMED: gate vs no-gate]\n" + format_leaderboard(breakdown(conf, "variant")))  # noqa: T201

    # the key diagnostic: confirmed-reversal expectancy per VIX bucket (default variant)
    print("\n[reversal CONFIRMED by VIX bucket (default variant)]")  # noqa: T201
    hdr = f"{'vix_bucket':<12}{'expectancy_r':>14}{'95%_low':>10}{'win_rate':>10}{'closed':>8}{'clusters':>10}"
    print(hdr + "\n" + "-" * len(hdr))  # noqa: T201
    by_bucket: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in conf:
        if t.variant == "default":
            by_bucket[str(t.vix_bucket)].append(t)
    for bucket in ("low", "mid", "high", "None"):
        g = by_bucket.get(bucket, [])
        if not [x for x in g if x.status == "closed"]:
            continue
        s = summarize(g)
        print(f"{bucket:<12}{s.expectancy_r:>14.3f}{s.expectancy_ci_low:>10.3f}"  # noqa: T201
              f"{s.win_rate:>10.2f}{s.n_closed:>8d}{s.n_clusters:>10d}")


if __name__ == "__main__":
    main()

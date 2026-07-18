"""Full-universe confirm of the high-volume reversal edge (rev_highvol).

Single selective variant (reversal_min_flip_rvol=1.3) over the whole basket, slip 0.0 and
0.05, reversal-only + CONFIRMED-only breakdowns. One variant per walk so 503 completes
reliably. Incremental flush so partial results survive a kill.

    python scripts/replay_rev_confirm.py [--limit 0]
"""

import argparse
import logging
from dataclasses import replace
from pathlib import Path

from _replay_common import load_replay_corpus
from swing_screener.analytics.performance import summarize
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import replay_book

log = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--slippage", default="0.0,0.05")
    ap.add_argument("--gate", choices=["highvol", "spring"], default="highvol")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    frames = load_replay_corpus(args.cache_dir, limit=args.limit)
    levels = [float(x) for x in args.slippage.split(",") if x.strip()]
    log.info("rev_highvol confirm: %d tickers, slips=%s ...", len(frames), levels)

    hdr = (f"{'book':<14}{'slip':>6}{'expectancy_r':>14}{'95%_low':>10}"
           f"{'win_rate':>10}{'closed':>8}{'clusters':>10}")
    print("\n" + hdr + "\n" + "-" * len(hdr), flush=True)  # noqa: T201
    for s in levels:
        base = StrategyConfig(fill_slippage_atr=s)
        gate = (replace(base, reversal_min_flip_rvol=1.3) if args.gate == "highvol"
                else replace(base, require_spring=True))
        trades = [t for t in replay_book(frames, timeframe="1d", base_cfg=base,
                                         variants={args.gate: gate})
                  if t.arm == "baseline" and t.play_type == "reversal"]
        for label, group in (("rev_all", trades),
                             ("rev_confirmed", [t for t in trades if t.strength == "confirmed"])):
            if not [g for g in group if g.status == "closed"]:
                continue
            r = summarize(group)
            print(f"{label:<14}{s:>6.2f}{r.expectancy_r:>14.3f}{r.expectancy_ci_low:>10.3f}"  # noqa: T201
                  f"{r.win_rate:>10.2f}{r.n_closed:>8d}{r.n_clusters:>10d}", flush=True)
        log.info("  done slip=%.2f", s)


if __name__ == "__main__":
    main()

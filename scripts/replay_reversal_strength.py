"""Capstone: does the reversal+no_flip edge concentrate in the CONFIRMED-strength subset,
and does THAT survive slippage? Runs the golden-master replay_book (momentum_flip_exit
=False) at several fill_slippage_atr levels and breaks the reversal book down by strength
(EARLY | CONFIRMED).

    python scripts/replay_reversal_strength.py [--limit N] [--slippage 0.0,0.05,0.10]
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
    parser.add_argument("--limit", type=int, default=0, help="0 = all tickers")
    parser.add_argument("--slippage", default="0.0,0.05,0.10")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    frames = load_replay_corpus(args.cache_dir, limit=args.limit)
    levels = [float(x) for x in args.slippage.split(",") if x.strip()]
    log.info("replaying %d tickers (reversal+no_flip) x %d slippage levels, by strength ...",
             len(frames), len(levels))

    hdr = (f"{'strength':<12}{'slip_atr':>9}{'expectancy_r':>14}{'95%_low':>10}"
           f"{'win_rate':>10}{'closed':>8}{'clusters':>10}")
    rows: list[tuple[str, str]] = []  # (strength, formatted_row)
    mix_confirmed: dict[str, list[float]] = {}
    for s in levels:
        base = StrategyConfig(momentum_flip_exit=False, fill_slippage_atr=s)
        trades = [t for t in replay_book(frames, timeframe="1d", base_cfg=base,
                                         variants={DEFAULT_VARIANT: base})
                  if t.arm == "baseline" and t.play_type == "reversal"]
        by_strength = breakdown(trades, "strength")
        for strength, summ in by_strength.items():
            rows.append((strength,
                         f"{strength:<12}{s:>9.2f}{summ.expectancy_r:>14.3f}"
                         f"{summ.expectancy_ci_low:>10.3f}{summ.win_rate:>10.2f}"
                         f"{summ.n_closed:>8d}{summ.n_clusters:>10d}"))
        if s == 0.0:
            mix_confirmed = _exit_mix([t for t in trades if t.strength == "confirmed"])
        log.info("  done slip=%.2f", s)

    print("\n" + hdr)        # noqa: T201
    print("-" * len(hdr))    # noqa: T201
    for want in ("confirmed", "early"):
        for strength, r in rows:
            if strength == want:
                print(r)     # noqa: T201

    total = sum(len(v) for v in mix_confirmed.values())
    print(f"\nCONFIRMED exit-reason mix @ slip 0.00 (n_closed={total}):")  # noqa: T201
    for reason, rs in sorted(mix_confirmed.items(), key=lambda kv: -len(kv[1])):
        share = len(rs) / total if total else 0
        avg = sum(rs) / len(rs) if rs else 0
        print(f"  {reason:<14} {len(rs):>5}  ({share:5.1%})  avg {avg:+.2f}R")  # noqa: T201


if __name__ == "__main__":
    main()

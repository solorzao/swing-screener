"""Throwaway experiment harness: A/B the outside-bar trigger and the entry-depth gate
against the incumbent, offline, over the cached daily basket.

Mirrors the diagnosis method (replay_book over .cache/1d) but swaps in a custom variant
set instead of build_screen_variants, so nothing touches the live shadow book. Prints the
variant leaderboard plus a per-variant exit-reason mix.

    python scripts/replay_experiment.py [--limit N] [--cache-dir .cache]

NOT shipped: a prototype to decide whether either lever earns a place in the live book.
"""

import argparse
import logging
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from _replay_common import load_replay_corpus
from swing_screener.analytics.performance import breakdown
from swing_screener.config import StrategyConfig
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.replay import (
    format_leaderboard,
    replay_book,
)

log = logging.getLogger(__name__)


def build_experiment_variants(base: StrategyConfig) -> dict[str, StrategyConfig]:
    """The incumbent plus the three video-derived entry experiments."""
    return {
        "default": base,
        "outside_bar": replace(base, trigger_kind="outside_bar"),
        "band_touch": replace(base, require_band_touch=True),
        "outside_bar_band": replace(base, trigger_kind="outside_bar", require_band_touch=True),
    }


def _exit_mix(trades: list[PaperTrade]) -> dict[str, dict[str, list[float]]]:
    """variant -> exit_reason -> [realized_r, ...] over CLOSED trades."""
    out: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for t in trades:
        if t.status == "closed" and t.realized_r is not None:
            out[t.variant][t.exit_reason or "?"].append(float(t.realized_r))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--limit", type=int, default=0, help="cap basket size (0 = all)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    frames = load_replay_corpus(args.cache_dir, limit=args.limit)
    log.info("replaying %d tickers x 4 variants over daily history ...", len(frames))

    base = StrategyConfig()
    variants = build_experiment_variants(base)
    trades = replay_book(frames, timeframe="1d", base_cfg=base, variants=variants)

    # CONTINUATION-ONLY: the outside_bar/band levers change only the continuation entry;
    # reversal trades are identical across variants and would dilute the comparison if mixed in.
    cont = [t for t in trades if t.arm == "baseline" and t.play_type == "continuation"]
    board = breakdown(cont, "variant")
    print("\n[continuation-only]\n" + format_leaderboard(board))  # noqa: T201

    print("\nexit-reason mix (continuation, closed, baseline exit):")  # noqa: T201
    mix = _exit_mix(cont)
    for vname in variants:
        reasons = mix.get(vname, {})
        total = sum(len(v) for v in reasons.values())
        print(f"\n  {vname}  (n_closed={total})")  # noqa: T201
        for reason, rs in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            share = len(rs) / total if total else 0
            avg = sum(rs) / len(rs) if rs else 0
            print(f"    {reason:<14} {len(rs):>5}  ({share:5.1%})  avg {avg:+.2f}R")  # noqa: T201


if __name__ == "__main__":
    main()

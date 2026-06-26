"""Continuation edge tournament (round 1): race the Tier-A quality gates head-to-head.

Each variant enables ONE detection gate at its recommended default; all run through the
golden-master replay_book and are compared CONTINUATION-ONLY (reversal trades are identical
across variants and would dilute the leaderboard). Prints the variant leaderboard plus the
exit-reason mix so we can watch the immediate-fade (momentum_flip) share fall.

    python scripts/replay_tournament.py [--limit N] [--slippage 0.0]
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
from swing_screener.signals.frame import build_frame  # noqa: F401  (ensures frame import path)

log = logging.getLogger(__name__)


def build_tournament_variants(base: StrategyConfig) -> dict[str, StrategyConfig]:
    """Control + one variant per Tier-A gate at its recommended default threshold."""
    return {
        "default": base,
        "vol_thrust": replace(base, vol_thrust_min=1.3),
        "value_band": replace(base, require_value_band=True),
        "orderly": replace(base, require_orderly_pullback=True),
        "ema_sep": replace(base, min_ema_sep_atr=0.5),
        "macd_hook": replace(base, require_macd_hook=True),
        "rsi_floor": replace(base, rsi_min_trigger=40.0),
        "atr_pct": replace(base, min_atr_pct=0.015),
        "strong_body": replace(base, min_trigger_body_frac=0.5),
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
    parser.add_argument("--limit", type=int, default=120, help="0 = all tickers")
    parser.add_argument("--slippage", type=float, default=0.0, help="fill_slippage_atr for all variants")
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
    variants = build_tournament_variants(base)
    log.info("racing %d tickers x %d variants (slip=%.2f), continuation-only ...",
             len(frames), len(variants), args.slippage)
    trades = replay_book(frames, timeframe="1d", base_cfg=base, variants=variants)

    cont = [t for t in trades if t.arm == "baseline" and t.play_type == "continuation"]
    board = breakdown(cont, "variant")
    print("\n[continuation-only leaderboard]\n" + format_leaderboard(board))  # noqa: T201

    print("\nmomentum_flip share by variant (the immediate-fade tell):")  # noqa: T201
    by_variant: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in cont:
        by_variant[t.variant].append(t)
    for vname in variants:
        mix = _exit_mix(by_variant.get(vname, []))
        total = sum(len(v) for v in mix.values())
        flip = mix.get("momentum_flip", [])
        tgt = mix.get("target", [])
        fshare = len(flip) / total if total else 0
        favg = sum(flip) / len(flip) if flip else 0
        tshare = len(tgt) / total if total else 0
        print(f"  {vname:<13} n={total:<5} flip {fshare:5.1%} @ {favg:+.2f}R   target {tshare:5.1%}")  # noqa: T201


if __name__ == "__main__":
    main()

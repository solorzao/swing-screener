"""Are the two reversal conviction filters additive or redundant? (promotion design input)

Races default vs rev_highvol (volume) vs spring (structure) vs both, reversal-only.

    python scripts/replay_rev_combo.py [--limit 250] [--slippage 0.0]

Pinned + sharded recipe (the 2026-07-04 revalidation: full universe, corpus pinned by
fetch date, one process per shard, then aggregate with the repo's clustered bounds --
see docs/plans/2026-07-04-revalidation-and-mae-studies.md):

    python scripts/replay_rev_combo.py --as-of 20260703 --slippage 0.05 \
        --shard 0/8 --out .cache/rev_combo        # x8 shards, x2 slippages
    python scripts/replay_rev_combo.py --aggregate .cache/rev_combo
"""

import argparse
import glob
import logging
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pandas as pd

from swing_screener.analytics.performance import breakdown

if TYPE_CHECKING:
    from swing_screener.db.models import PaperTrade
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import (
    _load_cached_daily,
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


class _Row:
    """Minimal trade-like object so ``breakdown`` can grade a parquet dump row."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, _: str) -> None:
        return None


def _unique_tickers(cache_dir: Path) -> list[str]:
    return sorted({p.name.split("_")[0] for p in (cache_dir / "1d").glob("*.parquet")}
                  - {"^VIX", "SPY"})


def _variants(base: StrategyConfig) -> dict[str, StrategyConfig]:
    return {
        "default": base,
        "highvol": replace(base, reversal_min_flip_rvol=1.3),
        "spring": replace(base, require_spring=True),
        "highvol_spring": replace(base, reversal_min_flip_rvol=1.3, require_spring=True),
    }


def _aggregate(dump_dir: Path) -> None:
    """Grade the shard dumps: per slippage x variant, full book + CONFIRMED cohort with
    the repo's ticker-clustered 2.5th-pct lower bound (breakdown owns the stats)."""
    for slip in ("0.05", "0.10"):
        files = sorted(glob.glob(str(dump_dir / f"REVCOMBO_s*_slip{slip}.parquet")))
        if not files:
            print(f"slippage {slip}: no shard dumps in {dump_dir}")  # noqa: T201
            continue
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        print(f"\n=== slippage {slip} | {len(files)} shards | {len(df)} reversal rows "  # noqa: T201
              f"| {df.ticker.nunique()} tickers ===")
        print(f"{'variant':<16}{'cohort':<11}{'exp R':>8}{'ci_low':>9}{'n_closed':>9}"  # noqa: T201
              f"{'clusters':>9}{'fill%':>7}")
        for variant, vd in df.groupby("variant"):
            for cohort, cd in (("full", vd), ("confirmed", vd[vd.strength == "confirmed"])):
                rows = [_Row(ticker=r.ticker, status=r.status, fill_status=r.fill_status,
                             realized_r=r.realized_r, variant="x") for r in cd.itertuples()]
                s = breakdown(cast("list[PaperTrade]", rows), "variant")["x"]
                print(f"{variant:<16}{cohort:<11}{s.expectancy_r:>+8.3f}"  # noqa: T201
                      f"{s.expectancy_ci_low:>+9.3f}{s.n_closed:>9d}{s.n_clusters:>9d}"
                      f"{s.fill_rate:>7.0%}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    ap.add_argument("--limit", type=int, default=250)
    ap.add_argument("--slippage", type=float, default=0.0)
    ap.add_argument("--as-of", default=None, metavar="YYYYMMDD",
                    help="pin the corpus to cache snapshots at/before this fetch date")
    ap.add_argument("--shard", default=None, metavar="I/N",
                    help="process tickers[i::n] and dump the trades to --out")
    ap.add_argument("--out", type=Path, default=Path(".cache/rev_combo"),
                    help="shard-dump directory (with --shard) / aggregate source")
    ap.add_argument("--aggregate", type=Path, default=None, metavar="DIR",
                    help="grade previously-dumped shards in DIR and exit (no replay)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.aggregate is not None:
        _aggregate(args.aggregate)
        return

    tickers = _unique_tickers(args.cache_dir)
    shard_i = None
    if args.shard:
        shard_i, shard_n = (int(x) for x in args.shard.split("/"))
        tickers = tickers[shard_i::shard_n]
    elif args.limit:
        tickers = tickers[: args.limit]

    frames: dict[str, pd.DataFrame] = {}
    for t in tickers:
        df = _load_cached_daily(t, args.cache_dir, args.as_of)
        if df is not None and len(df) > 60:
            frames[t] = df
    spy = _load_cached_daily("SPY", args.cache_dir, args.as_of)
    vix = _load_cached_daily("^VIX", args.cache_dir, args.as_of)
    if args.as_of and (shard_i in (0, None)):
        log.info("%s", corpus_stamp(args.cache_dir, tickers, args.as_of))
    log.info("rev combo: %d tickers, slip=%.2f%s ...", len(frames), args.slippage,
             f", shard {args.shard}" if args.shard else "")

    base = StrategyConfig(fill_slippage_atr=args.slippage)
    trades = replay_book(frames, timeframe="1d", base_cfg=base, variants=_variants(base),
                         spy_daily=spy, vix_daily=vix)
    rev = [t for t in trades if t.arm == "baseline" and t.play_type == "reversal"]

    if args.shard:
        args.out.mkdir(parents=True, exist_ok=True)
        out = args.out / f"REVCOMBO_s{shard_i}_slip{args.slippage:.2f}.parquet"
        pd.DataFrame([{c: getattr(t, c) for c in _DUMP_COLS} for t in rev]).to_parquet(out)
        log.info("shard %s done: %d reversal rows -> %s", args.shard, len(rev), out.name)
        return
    print("\n[reversal conviction filters: additive?]\n"  # noqa: T201
          + format_leaderboard(breakdown(rev, "variant")))


if __name__ == "__main__":
    main()

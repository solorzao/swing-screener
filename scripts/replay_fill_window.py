"""Full-universe A/B of the reversal FILL WINDOW: one-bar (legacy) vs multi-bar resting limit.

The 2026-07 audit found ~95% of confirmed reversals unfillable in the one-bar window and
the fills adversely selected (live -0.86R vs the replay-promised +0.125R). This re-runs
the confirmed-reversal edge evaluation under the new resting-limit fill model (pending ->
filled/invalidated/expired, see pipeline.shadow.resolve_pending) so the edge claim rests
on fills a trader could actually get. Net of the 0.05 ATR haircut (the config default).

    python scripts/replay_fill_window.py [--limit 0] [--windows 1,5]
"""

import argparse
import logging
from pathlib import Path

import pandas as pd

from swing_screener.analytics.performance import summarize
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import _load_cached_daily, replay_book

log = logging.getLogger(__name__)


def _unique_tickers(cache_dir: Path) -> list[str]:
    return sorted({p.name.split("_")[0] for p in (cache_dir / "1d").glob("*.parquet")}
                  - {"^VIX", "SPY"})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--windows", default="1,5")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    tickers = _unique_tickers(args.cache_dir)
    if args.limit:
        tickers = tickers[: args.limit]
    frames: dict[str, pd.DataFrame] = {}
    for t in tickers:
        df = _load_cached_daily(t, args.cache_dir)
        if df is not None and len(df) > 60:
            frames[t] = df
    windows = [int(x) for x in args.windows.split(",") if x.strip()]
    log.info("fill-window A/B: %d tickers, windows=%s, slip=0.05 ...", len(frames), windows)

    hdr = (f"{'book':<16}{'window':>7}{'expectancy_r':>14}{'95%_low':>10}{'win_rate':>10}"
           f"{'closed':>8}{'clusters':>10}{'fill_rate':>11}{'n_total':>9}")
    print("\n" + hdr + "\n" + "-" * len(hdr), flush=True)  # noqa: T201
    for w in windows:
        base = StrategyConfig(fill_slippage_atr=0.05, reversal_fill_window_bars=w)
        trades = [t for t in replay_book(frames, timeframe="1d", base_cfg=base,
                                         variants={"default": base})
                  if t.arm == "baseline" and t.play_type == "reversal"]
        for label, group in (("rev_all", trades),
                             ("rev_confirmed", [t for t in trades if t.strength == "confirmed"])):
            if not [g for g in group if g.status == "closed"]:
                continue
            r = summarize(group)
            print(f"{label:<16}{w:>7d}{r.expectancy_r:>14.3f}{r.expectancy_ci_low:>10.3f}"  # noqa: T201
                  f"{r.win_rate:>10.2f}{r.n_closed:>8d}{r.n_clusters:>10d}"
                  f"{r.fill_rate:>11.3f}{r.n_total:>9d}", flush=True)
        log.info("  done window=%d", w)


if __name__ == "__main__":
    main()

"""Edge-discovery exp 1: free regime breakdown. Where does each book's edge live?

Runs the golden-master replay_book with spy_daily so every fill is stamped with the
point-in-time SPY regime (market_trend bull/bear, market_vol calm/elevated/high), then breaks
down expectancy by regime cell x play_type. No new code -- pure diagnostic. Informs the
RS/regime gates (our own diagnosis warns the 200DMA split is too coarse, so confirm the SIGN).

    python scripts/replay_regime.py [--limit 250]
"""

import argparse
import logging
from collections import defaultdict
from pathlib import Path

import pandas as pd

from swing_screener.analytics.performance import summarize
from swing_screener.config import StrategyConfig
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.replay import _load_cached_daily, replay_book

log = logging.getLogger(__name__)


def _unique_tickers(cache_dir: Path) -> list[str]:
    return sorted({p.name.split("_")[0] for p in (cache_dir / "1d").glob("*.parquet")})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--limit", type=int, default=250, help="0 = all tickers")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    tickers = _unique_tickers(args.cache_dir)
    if args.limit:
        tickers = tickers[: args.limit]
    frames: dict[str, pd.DataFrame] = {}
    for t in tickers:
        df = _load_cached_daily(t, args.cache_dir)
        if df is not None and len(df) > 60:
            frames[t] = df
    spy = _load_cached_daily("SPY", args.cache_dir)
    if spy is None:
        log.error("no SPY in cache; cannot stamp regime")
        return
    log.info("regime breakdown: %d tickers (spy-stamped) ...", len(frames))

    base = StrategyConfig()
    trades = [t for t in replay_book(frames, timeframe="1d", base_cfg=base,
                                     variants={"default": base}, spy_daily=spy)
              if t.arm == "baseline"]

    hdr = (f"{'play_type':<14}{'cell':<18}{'expectancy_r':>14}{'95%_low':>10}"
           f"{'win_rate':>10}{'closed':>8}{'clusters':>10}")
    for dim in ("market_trend", "market_vol"):
        print(f"\n=== by {dim} ===\n{hdr}\n{'-' * len(hdr)}")  # noqa: T201
        cells: dict[tuple[str, str], list[PaperTrade]] = defaultdict(list)
        for t in trades:
            cells[(t.play_type, str(getattr(t, dim)))].append(t)
        for (pt, cell), group in sorted(cells.items()):
            closed = [g for g in group if g.status == "closed"]
            if not closed:
                continue
            s = summarize(group)
            print(f"{pt:<14}{cell:<18}{s.expectancy_r:>14.3f}{s.expectancy_ci_low:>10.3f}"  # noqa: T201
                  f"{s.win_rate:>10.2f}{s.n_closed:>8d}{s.n_clusters:>10d}")


if __name__ == "__main__":
    main()

"""Same-bar fill+stop optimism bracket, reversal book (2026-07-04 review, study 3).

fill.py documents an ACCEPTED bias: a bar that sweeps both the resting limit and the stop
resolves as "filled" (intrabar ordering unknowable from OHLC), and the advance stepper's
same-bucket guard means the stop is first checked the NEXT bar. Worst-shaped for reversal:
the limit rests near the washout low. This measures how much of the reversal expectancy
that donates: reconstruct each trade's original stop as entry_price - risk, look up the
FILL BAR's low in the pinned corpus, flag trades where low <= stop (the bar demonstrably
touched both), re-settle flagged trades at the book's own mean stop-exit R, and report the
honest [pessimistic, optimistic] bracket. Results + interpretation:
docs/plans/2026-07-04-revalidation-and-mae-studies.md.

    python scripts/replay_fill_stop_bracket.py [--dump-dir .cache/queue_experiments] \
        [--cache-dir .cache] [--as-of 20260703]
"""

import argparse
import glob
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pandas as pd

from swing_screener.analytics.performance import breakdown
from swing_screener.pipeline.replay import _cached_daily_file

if TYPE_CHECKING:
    from swing_screener.db.models import PaperTrade


class _Row:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, _: str) -> None:
        return None


def load_rev(dump_dir: Path, slip: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(dump_dir / f"R_target_s*_slip{slip}.parquet")))
    if not files:
        raise SystemExit(f"no R_target dumps in {dump_dir} -- run replay_queue_experiments.py first")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    return df[(df.play_type == "reversal") & (df.variant == "retrace_100")
              & (df.status == "closed") & (df.fill_status == "filled")
              & df.realized_r.notna()].copy()


def fill_bar_lows(df: pd.DataFrame, cache_dir: Path, as_of: str) -> pd.Series:
    """Low of each trade's fill bar from the pinned per-ticker parquet."""
    lows = pd.Series(index=df.index, dtype=float)
    for ticker, sub in df.groupby("ticker"):
        f = _cached_daily_file(str(ticker), cache_dir, as_of)
        if f is None:
            continue
        bars = pd.read_parquet(f)
        idx = pd.to_datetime(bars.index).normalize()
        low_by_date = pd.Series(bars["low"].values, index=idx)
        dates = pd.to_datetime(sub.entry_date).dt.normalize()
        lows.loc[sub.index] = low_by_date.reindex(dates).values
    return lows


def bracket(df: pd.DataFrame, label: str, cache_dir: Path, as_of: str) -> None:
    stop_mean = df.loc[df.exit_reason == "stop", "realized_r"].mean()
    df = df.assign(orig_stop=df.entry_price - df.risk)
    df["fill_low"] = fill_bar_lows(df, cache_dir, as_of)
    known = df.dropna(subset=["fill_low"])
    flagged = known.fill_low <= known.orig_stop
    print(f"\n=== {label} ===")  # noqa: T201
    print(f"n={len(known)} (fill-bar low resolved for {len(known)}/{len(df)}), "  # noqa: T201
          f"stop-exit mean={stop_mean:+.4f}R")
    fl = known[flagged]
    print(f"flagged (fill bar touched stop): {flagged.sum()} = {flagged.mean():.2%}; "  # noqa: T201
          "actual outcomes: "
          + ", ".join(f"{k}={v}" for k, v in fl.exit_reason.value_counts().items())
          + f"; actual mean={fl.realized_r.mean():+.3f}R")

    pess = known.realized_r.where(~flagged, stop_mean)
    for name, mask in (("full book", pd.Series(True, index=known.index)),
                       ("CONFIRMED", known.strength == "confirmed")):
        opt_objs = [_Row(ticker=t, status="closed", fill_status="filled", realized_r=r,
                         variant="x")
                    for t, r in zip(known.ticker[mask], known.realized_r[mask])]
        pes_objs = [_Row(ticker=t, status="closed", fill_status="filled", realized_r=r,
                         variant="x")
                    for t, r in zip(known.ticker[mask], pess[mask])]
        o = breakdown(cast("list[PaperTrade]", opt_objs), "variant")["x"]
        p = breakdown(cast("list[PaperTrade]", pes_objs), "variant")["x"]
        n_fl = int((flagged & mask).sum())
        print(f"{name:<10} n={int(mask.sum()):>6d} flagged={n_fl:>5d} "  # noqa: T201
              f"({n_fl / max(int(mask.sum()), 1):.1%})  "
              f"optimistic {o.expectancy_r:+.4f} (lb {o.expectancy_ci_low:+.4f})  "
              f"pessimistic {p.expectancy_r:+.4f} (lb {p.expectancy_ci_low:+.4f})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dump-dir", type=Path, default=Path(".cache/queue_experiments"))
    ap.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    ap.add_argument("--as-of", default="20260703", metavar="YYYYMMDD")
    args = ap.parse_args()

    for slip in ("0.05", "0.10"):
        bracket(load_rev(args.dump_dir, slip), f"reversal retrace_100, slippage {slip}",
                args.cache_dir, args.as_of)
    print("\nNote: 'pessimistic' re-settles every flagged trade at the book's mean "  # noqa: T201
          "stop-exit R -- the true value lies inside the bracket (intrabar ordering "
          "unknowable from OHLC).")


if __name__ == "__main__":
    main()

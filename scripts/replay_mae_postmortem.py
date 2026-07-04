"""MAE post-mortem on the queued-experiment dumps (2026-07-04 review, study 1).

Consumes the per-trade parquet dumps scripts/replay_queue_experiments.py wrote (low_water /
high_water populated by the shared advance_open stepper) -- no replay needed. Answers the
pre-registered entry-economics questions for continuation and the stop-width / target /
breakeven questions for reversal. Results + interpretation:
docs/plans/2026-07-04-revalidation-and-mae-studies.md.

    python scripts/replay_mae_postmortem.py [--dump-dir .cache/queue_experiments]

  Q1 MAE-in-R distribution by outcome, continuation closed+filled book.
  Q2 Limit-entry capture curve: share of trades whose post-entry path reached
     entry - x*risk (a LOWER BOUND on a resting-limit fill: the fill bar's own low is
     excluded by the advance stepper's same-bucket guard).
  Q3 Counterfactual limit-entry book at depth x: filled cohort re-priced via
     new_R = (old_R + x) / (1 - x) (same exit PRICE, better entry, tighter risk);
     misses become no-trades. APPROXIMATION: exit timing not re-simulated, same-bar
     fill+stop bias unchanged. Adverse selection IS included -- deep-retracing trades
     keep their (worse) outcomes; that is the point of the exercise.
  Q4 Stop-width room: share of WINNERS that nearly stopped (MAE >= 0.7/0.8/0.9R).
  Q5 Target reachability: share of filled trades whose high_water touched the target.
  Q6 Breakeven give-back: share of trades that saw >= +1R (high_water) yet closed <= 0
     (the be_1r rationale number).
"""

import argparse
import glob
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pandas as pd

from swing_screener.analytics.performance import breakdown

if TYPE_CHECKING:
    from swing_screener.db.models import PaperTrade


class _Row:
    """Minimal trade-like object for analytics.performance.breakdown."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, _: str) -> None:
        return None


def load(dump_dir: Path, prefix: str, slip: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(dump_dir / f"{prefix}_s*_slip{slip}.parquet")))
    if not files:
        raise SystemExit(f"no {prefix} dumps in {dump_dir} -- run replay_queue_experiments.py first")
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def closed_filled(df: pd.DataFrame) -> pd.DataFrame:
    return df[(df.status == "closed") & (df.fill_status == "filled")
              & df.realized_r.notna()].copy()


def add_mae(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["mae_r"] = (df.entry_price - df.low_water) / df.risk
    df["mfe_r"] = (df.high_water - df.entry_price) / df.risk
    return df


def q1_mae_by_outcome(cont: pd.DataFrame) -> None:
    print("\n=== Q1: continuation MAE-in-R by outcome (closed+filled) ===")  # noqa: T201
    d = cont.dropna(subset=["mae_r"]).assign(win=lambda x: x.realized_r > 0)
    g = d.groupby("win").mae_r.describe(percentiles=[0.25, 0.5, 0.75, 0.9])
    print(g[["count", "mean", "25%", "50%", "75%", "90%"]].round(3).to_string())  # noqa: T201
    g2 = d.groupby("exit_reason").agg(n=("mae_r", "size"), mean_mae=("mae_r", "mean"),
                                      med_mae=("mae_r", "median"),
                                      mean_r=("realized_r", "mean")).round(3)
    print("\nby exit_reason:\n" + g2.to_string())  # noqa: T201


def q2_q3_capture_and_counterfactual(cont: pd.DataFrame) -> None:
    print("\n=== Q2/Q3: limit-entry capture curve + counterfactual book (continuation) ===")  # noqa: T201
    d = cont.dropna(subset=["mae_r"]).copy()
    print(f"baseline: n={len(d)} mean={d.realized_r.mean():+.3f}R")  # noqa: T201
    print(f"{'depth x':>8}{'fill%':>8}{'win fill%':>10}{'n_fill':>8}{'cf mean R':>10}"  # noqa: T201
          f"{'cf ci_low':>10}{'clusters':>9}")
    wins = d.realized_r > 0
    for x in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.65, 0.80):
        filled = d.mae_r >= x
        cf_r = (d.realized_r[filled] + x) / (1 - x)
        objs = [_Row(ticker=t, status="closed", fill_status="filled", realized_r=r,
                     variant="cf") for t, r in zip(d.ticker[filled], cf_r)]
        objs += [_Row(ticker=t, status="closed", fill_status="missed", realized_r=None,
                      variant="cf") for t in d.ticker[~filled]]
        s = breakdown(cast("list[PaperTrade]", objs), "variant")["cf"]
        win_capture = (filled & wins).sum() / max(wins.sum(), 1)
        print(f"{x:>8.2f}{filled.mean():>8.1%}{win_capture:>10.1%}{int(filled.sum()):>8d}"  # noqa: T201
              f"{s.expectancy_r:>+10.3f}{s.expectancy_ci_low:>+10.3f}{s.n_clusters:>9d}")
    print("(capture is a LOWER bound -- the fill bar's own low is excluded)")  # noqa: T201


def q4_stop_room(cont: pd.DataFrame, rev: pd.DataFrame) -> None:
    print("\n=== Q4: winners that nearly stopped (MAE >= threshold) ===")  # noqa: T201
    for name, d in (("continuation", cont),
                    ("reversal(retrace_100)", rev),
                    ("reversal CONFIRMED", rev[rev.strength == "confirmed"])):
        w = d.dropna(subset=["mae_r"])
        w = w[w.realized_r > 0]
        line = f"{name:<24} n_win={len(w):>6d}  "
        for th in (0.7, 0.8, 0.9):
            line += f">={th}: {(w.mae_r >= th).mean():>6.1%}  "
        print(line)  # noqa: T201


def q5_target_reach(cont: pd.DataFrame, rev_all: pd.DataFrame) -> None:
    print("\n=== Q5: target reachability (high_water >= target, closed+filled) ===")  # noqa: T201
    c = cont.dropna(subset=["high_water"])
    print(f"continuation: {(c.high_water >= c.target).mean():.1%} of {len(c)}")  # noqa: T201
    for v, d in rev_all.groupby("variant"):
        d = closed_filled(d).dropna(subset=["high_water"])
        conf = d[d.strength == "confirmed"]
        print(f"reversal {v:<13}: {(d.high_water >= d.target).mean():.1%} of {len(d)}"  # noqa: T201
              f"  (confirmed: {(conf.high_water >= conf.target).mean():.1%})")


def q6_giveback(cont: pd.DataFrame, rev: pd.DataFrame) -> None:
    print("\n=== Q6: saw >= +1R (high_water) but closed <= 0 (be_1r rationale) ===")  # noqa: T201
    for name, d in (("continuation", cont), ("reversal(retrace_100)", rev),
                    ("reversal CONFIRMED", rev[rev.strength == "confirmed"])):
        d = d.dropna(subset=["mfe_r"])
        saw1r = d[d.mfe_r >= 1.0]
        gave = (saw1r.realized_r <= 0).mean() if len(saw1r) else float("nan")
        print(f"{name:<24} saw+1R: {len(saw1r):>6d} ({len(saw1r) / len(d):.1%} of book)  "  # noqa: T201
              f"closed<=0 after: {gave:.1%}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dump-dir", type=Path, default=Path(".cache/queue_experiments"))
    ap.add_argument("--slippage", default="0.05")
    args = ap.parse_args()

    d_dump = load(args.dump_dir, "D_dump", args.slippage)
    cont = add_mae(closed_filled(d_dump[d_dump.play_type == "continuation"]))
    r_all = load(args.dump_dir, "R_target", args.slippage)
    rev100 = add_mae(closed_filled(
        r_all[(r_all.play_type == "reversal") & (r_all.variant == "retrace_100")]))

    print(f"continuation closed+filled n={len(cont)} mean={cont.realized_r.mean():+.4f}R")  # noqa: T201
    print(f"reversal retrace_100 closed+filled n={len(rev100)} "  # noqa: T201
          f"mean={rev100.realized_r.mean():+.4f}R")

    q1_mae_by_outcome(cont)
    q2_q3_capture_and_counterfactual(cont)
    q4_stop_room(cont, rev100)
    q5_target_reach(cont, r_all[r_all.play_type == "reversal"])
    q6_giveback(cont, rev100)


if __name__ == "__main__":
    main()

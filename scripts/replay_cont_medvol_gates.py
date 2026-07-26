"""Q3 cont_medvol_strict_gate: does a strict med-vol continuation cut reach lb > 0?

The med-vol continuation slice is the last selection-side open question in
edge/continuation.md (-0.14R, lb -0.20 on the reflect corpus). This replays the
pinned-corpus analogue: D_dump default/continuation/volatility_tier=med/closed rows,
with detector context recomputed at every booked trade's trigger bar (the
replay_cont_rank_sweep.py recompute + merge pattern -- no lookahead, only bars up to
the signal bar).

PRE-REGISTERED GRID (fixed, no post-hoc tuning), 6 comparisons, Bonferroni x6
one-sided:

- 4 cells: ma_rising AND depth >= d AND trend_dist >= t, (d, t) in {0.5, 1.0} x {1.0, 2.0}
- 2 marginals: ma_rising only; depth >= 1.0 only

where depth = (ema_fast - swing_low)/atr, trend_dist = (trigger_close - ema_slow)/atr,
ma_rising = ema_slow rising over the last 5 bars (iloc[-1] > iloc[-6]).

DECISION RULE: ESCALATE iff any cell's Bonferroni-corrected clustered one-sided 95%
CI lower bound > 0 (net 0.05 ATR, already baked into realized_r) with n_closed >= 20
and clusters >= 8. Otherwise PARK (report the null).

Needs the D_dump books from scripts/replay_queue_experiments.py:

    python scripts/replay_cont_medvol_gates.py --cache-root .cache
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
from types import SimpleNamespace

import pandas as pd

from swing_screener.analytics.performance import (
    _clustered_ci_low,
    clustered_two_sample_delta_low,
    summarize,
)
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import load_cached_daily
from swing_screener.signals.detect import detect_last_bar
from swing_screener.signals.frame import build_frame

AS_OF = "20260703"
SLIP = "0.05"
CFG = StrategyConfig()

# Pre-registered comparison family: 4 grid cells + 2 marginal single-gate cuts.
FAMILY = 6
ALPHA = 0.05  # one-sided
CORR_PCT = 100.0 * ALPHA / FAMILY  # Bonferroni-corrected bootstrap percentile (~0.833)
_Z_CORR = NormalDist().inv_cdf(1.0 - ALPHA / FAMILY)  # IID fallback quantile (~2.394)


def load_book(cache_root: Path) -> pd.DataFrame:
    books = cache_root / "queue_experiments"
    frames = [pd.read_parquet(p) for p in books.glob(f"D_dump*_slip{SLIP}.parquet")]
    df = pd.concat(frames, ignore_index=True)
    return df[(df["variant"] == "default") & (df["play_type"] == "continuation")
              & (df["volatility_tier"] == "med") & (df["status"] == "closed")].copy()


def compute_features(signals: pd.DataFrame, cache_root: Path) -> pd.DataFrame:
    """Detector context at each booked trade's trigger bar (bars strictly before
    opened_date -- the replay_cont_rank_sweep.py recompute, no lookahead)."""
    rows = []
    tickers = sorted(signals["ticker"].unique())
    for i, ticker in enumerate(tickers):
        if i % 50 == 0:
            print(f"  features: ticker {i}/{len(tickers)}", file=sys.stderr, flush=True)
        grp = signals[signals["ticker"] == ticker]
        raw = load_cached_daily(ticker, cache_root, AS_OF)
        if raw is None:
            continue
        f = build_frame(raw, CFG)
        dates = f.index.date
        for od in sorted(set(grp["opened_date"])):
            fa = f[dates < od]
            if len(fa) < 60:
                continue
            ctx = detect_last_bar(fa, CFG)
            if ctx is None:
                continue
            last = fa.iloc[-1]
            atr = ctx.atr or 1e-9
            rows.append({
                "ticker": ticker, "opened_date": od,
                "depth": (float(last["ema_fast"]) - ctx.swing_low) / atr,
                "trend_dist": (ctx.trigger_close - float(last["ema_slow"])) / atr,
                "ma_rising": bool(fa["ema_slow"].iloc[-1] > fa["ema_slow"].iloc[-6]),
            })
    return pd.DataFrame(rows)


def by_ticker(df: pd.DataFrame) -> dict[str, list[float]]:
    closed = df[(df["status"] == "closed") & (df["fill_status"] == "filled")
                & df["realized_r"].notna()]
    out: dict[str, list[float]] = defaultdict(list)
    for t, r in zip(closed["ticker"], closed["realized_r"]):
        out[t].append(float(r))
    return out


def corrected_low(df: pd.DataFrame) -> float:
    """Bonferroni-corrected one-sided clustered lower bound on mean R (the decision
    number): the house clustered bootstrap at the corrected percentile, never more
    optimistic than the corrected-IID bound (mirrors summarize's 2.5th-pct bound)."""
    pools = by_ticker(df)
    realized = [r for rs in pools.values() for r in rs]
    n = len(realized)
    if n == 0:
        return float("nan")
    mean = sum(realized) / n
    if n >= 2:
        sd = pd.Series(realized).std(ddof=1)
        iid_low = mean - _Z_CORR * float(sd) / (n ** 0.5)
    else:
        iid_low = mean
    low, _, _ = _clustered_ci_low(pools, iid_low, lower_pct=CORR_PCT)
    return low


def line(name: str, df: pd.DataFrame) -> str:
    s = summarize([SimpleNamespace(**r) for r in df.to_dict("records")])
    return (f"{name:<42} exp={s.expectancy_r:+.3f} lb2.5={s.expectancy_ci_low:+.3f} "
            f"lb_corr={corrected_low(df):+.3f} n={s.n_closed} clus={s.n_clusters} "
            f"win={s.win_rate:.2f}")


def cell_masks(df: pd.DataFrame) -> dict[str, pd.Series]:
    """The pre-registered family, in registration order (4 cells + 2 marginals)."""
    out: dict[str, pd.Series] = {}
    for d in (0.5, 1.0):
        for t in (1.0, 2.0):
            out[f"ma_rising & depth>={d} & trend_dist>={t}"] = (
                df["ma_rising"] & (df["depth"] >= d) & (df["trend_dist"] >= t))
    out["ma_rising (marginal)"] = df["ma_rising"]
    out["depth>=1.0 (marginal)"] = df["depth"] >= 1.0
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-root", type=Path, default=Path(".cache"))
    args = ap.parse_args()

    book = load_book(args.cache_root)
    print(f"substrate: {len(book)} closed med-vol continuation rows, "
          f"{book['ticker'].nunique()} tickers (slip {SLIP}, as-of {AS_OF})", flush=True)
    print(line("baseline med-vol (full substrate)", book), flush=True)

    feats = compute_features(book[["ticker", "opened_date"]].drop_duplicates(),
                             args.cache_root)
    df = book.merge(feats, on=["ticker", "opened_date"], how="inner")
    print(f"features recomputed for {len(feats)} signals; merged book {len(df)} rows "
          f"({len(df) / len(book):.0%} of substrate)", flush=True)

    print(f"\n[pre-registered family: {FAMILY} comparisons, Bonferroni x{FAMILY} "
          f"one-sided -> corrected percentile {CORR_PCT:.3f} (z={_Z_CORR:.3f})]",
          flush=True)
    print(line("baseline med-vol (merged, reference)", df), flush=True)
    for name, mask in cell_masks(df).items():
        gated, ungated = df[mask], df[~mask]
        print(f"\n{name}", flush=True)
        print("  " + line("PASS (gated)", gated), flush=True)
        print("  " + line("FAIL (complement)", ungated), flush=True)
        d_lo = clustered_two_sample_delta_low(by_ticker(gated), by_ticker(ungated))
        print(f"  pass-vs-fail clustered delta lb2.5={d_lo:+.3f} "
              f"(descriptive, uncorrected)", flush=True)


if __name__ == "__main__":
    main()

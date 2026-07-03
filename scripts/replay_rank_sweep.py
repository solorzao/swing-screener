"""Ranking-key sweep over the confirm3 confirmed book: does ANY ordering separate?

The score never gates, so the trade book is fixed; ranking quality is a pure post-hoc
question. For every booked confirmed trade, recompute the detector context at its
trigger bar (features), then evaluate candidate orderings three ways:
  1. quintile expectancy ladder (monotonic separation?)
  2. top-quintile vs rest, ticker-clustered two-sample delta lower bound
  3. digest simulation: per opened_date, top-5 by key -> selected-vs-rest delta,
     per-pick value (missed picks count 0R), fill rate.
Run for slip 0.05 and 0.10 books (same signals; realized_r differs).
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from swing_screener.analytics.performance import (
    clustered_two_sample_delta_low,
    summarize,
)
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import _load_cached_daily
from swing_screener.signals.frame import build_frame
from swing_screener.signals.reversal import detect_reversal

BOOKS = Path(".cache/rotation_entry")
CACHE = Path(".cache")
CFG = StrategyConfig()  # window=3 on this branch == the confirm3 book's config


def load_book(slip: str) -> pd.DataFrame:
    frames = [pd.read_parquet(p) for p in BOOKS.glob(f"C_robust*_slip{slip}.parquet")]
    df = pd.concat(frames, ignore_index=True)
    return df[(df["variant"] == "confirm3") & (df["strength"] == "confirmed")].copy()


def confirm_lag(f: pd.DataFrame) -> int:
    g = 0
    while g < len(f) - 1 and bool(f.iloc[-1 - g]["bullish"]):
        g += 1
    return max(g - 1, 0)


def compute_features(signals: pd.DataFrame) -> pd.DataFrame:
    """One row per (ticker, opened_date): the detector context at the trigger bar."""
    rows = []
    for ticker, grp in signals.groupby("ticker"):
        raw = _load_cached_daily(ticker, CACHE)
        if raw is None:
            continue
        f = build_frame(raw, CFG)
        dates = f.index.date
        for od in sorted(set(grp["opened_date"])):
            fa = f[dates < od]
            if len(fa) < 60:
                continue
            ctx = detect_reversal(fa, CFG)
            if ctx is None or ctx.strength != "confirmed":
                continue
            rows.append({
                "ticker": ticker, "opened_date": od,
                "rvol": ctx.volume_ratio, "body_frac": ctx.body_frac,
                "shaved": ctx.shaved_bottom, "red_run": ctx.red_run,
                "min_rsi": ctx.min_rsi, "rsi": ctx.rsi, "spring": ctx.is_spring,
                "atr_pct": ctx.atr / ctx.trigger_close if ctx.trigger_close else 0.0,
                "lag": confirm_lag(fa),
            })
    return pd.DataFrame(rows)


def keys(df: pd.DataFrame) -> dict[str, pd.Series]:
    bounce = 0.6 * df["body_frac"].clip(0, 1) + 0.4 * df["shaved"].astype(float)
    depth = ((25.0 - df["min_rsi"]) / 25.0).clip(0, 1)
    return {
        "legacy_score": df["signal_score"],
        "rvol": df["rvol"],
        "lag_late_first": df["lag"] + df["rvol"].clip(0, 3) / 10.0,
        "bounce_quality": bounce,
        "downtrend": df["red_run"],
        "rsi_depth": depth,
        "spring_first": df["spring"].astype(float) + df["rvol"].clip(0, 3) / 10.0,
        "calm_first": -df["atr_pct"],
        "rvol_x_lag": df["rvol"].clip(0, 3) + df["lag"],
        "w_vol_conf": (df["rvol"] - 1).clip(0, 1) * 0.5 + bounce * 0.5,
        # the proposed new default weight vector: lag as a 6th score component
        # (lag normalized by the confirm window=3), volume bumped, depth dropped
        "proposed_w": (0.40 * (df["lag"] / 3.0).clip(0, 1)
                       + 0.30 * (df["rvol"] - 1).clip(0, 1)
                       + 0.10 * bounce + 0.10 * (df["red_run"] / 6.0).clip(0, 1)
                       + 0.10 * 1.0),
    }


def by_ticker(df: pd.DataFrame) -> dict[str, list[float]]:
    closed = df[(df["status"] == "closed") & (df["fill_status"] == "filled")
                & df["realized_r"].notna()]
    out: dict[str, list[float]] = defaultdict(list)
    for t, r in zip(closed["ticker"], closed["realized_r"]):
        out[t].append(float(r))
    return out


def stats(df: pd.DataFrame) -> tuple[float, float, int]:
    s = summarize([SimpleNamespace(**r) for r in df.to_dict("records")])
    return s.expectancy_r, s.expectancy_ci_low, s.n_closed


def main() -> None:
    for slip in ("0.05", "0.10"):
        book = load_book(slip)
        if slip == "0.05":
            feats = compute_features(book[["ticker", "opened_date"]].drop_duplicates())
            print(f"features computed for {len(feats)} signals "
                  f"(book has {len(book)})", file=sys.stderr)
        df = book.merge(feats, on=["ticker", "opened_date"], how="inner")
        print(f"\n================ slip {slip}  (n={len(df)}, "
              f"closed={int(((df['status'] == 'closed') & (df['fill_status'] == 'filled')).sum())}) ================")
        for name, key in keys(df).items():
            d = df.assign(_k=key)
            # 1) quintile ladder over closed fills
            closed = d[(d["status"] == "closed") & (d["fill_status"] == "filled")
                       & d["realized_r"].notna()].copy()
            closed["q"] = pd.qcut(closed["_k"].rank(method="first"), 5,
                                  labels=["q1", "q2", "q3", "q4", "q5"])
            ladder = [f"{closed[closed['q'] == q]['realized_r'].mean():+.3f}"
                      for q in ("q1", "q2", "q3", "q4", "q5")]
            # 2) top-quintile vs rest, clustered delta
            hi = closed[closed["q"] == "q5"]
            lo = closed[closed["q"] != "q5"]
            delta_lo = clustered_two_sample_delta_low(by_ticker(hi), by_ticker(lo))
            hi_exp, hi_lo, hi_n = stats(hi)
            # 3) digest sim: per-day top-5 (on ALL signals, misses included)
            d["_sel"] = False
            for _, day in d.groupby("opened_date"):
                top = day.nlargest(5, "_k").index
                d.loc[top, "_sel"] = True
            sel, uns = d[d["_sel"]], d[~d["_sel"]]
            sel_exp, sel_lo, sel_n = stats(sel)
            sel_fill = (sel["fill_status"] == "filled").mean()
            # per-pick value: realized R if filled+closed else 0 (undecided open ~ 0 too)
            pick_val = sel["realized_r"].where(
                (sel["status"] == "closed") & (sel["fill_status"] == "filled"), 0.0).mean()
            dig_delta = clustered_two_sample_delta_low(by_ticker(sel), by_ticker(uns))
            print(f"{name:<15} ladder={'/'.join(ladder)}  "
                  f"q5: exp={hi_exp:+.3f} lo={hi_lo:+.3f} n={hi_n} dLo={delta_lo:+.3f} | "
                  f"top5/day: exp={sel_exp:+.3f} lo={sel_lo:+.3f} n={sel_n} "
                  f"fill={sel_fill:.0%} pick_val={pick_val:+.4f} dLo={dig_delta:+.3f}")


if __name__ == "__main__":
    main()

"""Continuation rank sweep: does ANY ordering separate outcomes on the fixed book?

The continuation score's forward gradient is INVERTED (0.70-0.80 grades -0.44R while
0.50-0.60 is the only positive band) yet score rank is the sole digest ordering. The
score never gates, so ranking quality is post-hoc testable on the fixed default book
(the same method that produced reversal score v2): recompute the detector context at
every booked trade's trigger bar, then evaluate candidate orderings via quintile
ladders, top-quintile-vs-rest ticker-clustered delta bounds, and a per-day digest
top-5 simulation -- at 0.05 and 0.10 ATR slippage.

Also prints the free cohort reads (mtf_aligned, timeframe) the shadow book stamps but
nothing ever judged.

Needs the D_dump books from scripts/replay_queue_experiments.py.
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
from swing_screener.signals.detect import detect_last_bar
from swing_screener.signals.frame import build_frame

BOOKS = Path(".cache/queue_experiments")
CACHE = Path(".cache")
AS_OF = "20260703"
CFG = StrategyConfig()


def load_book(slip: str) -> pd.DataFrame:
    frames = [pd.read_parquet(p) for p in BOOKS.glob(f"D_dump*_slip{slip}.parquet")]
    df = pd.concat(frames, ignore_index=True)
    return df[(df["variant"] == "default") & (df["play_type"] == "continuation")].copy()


def compute_features(signals: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for ticker, grp in signals.groupby("ticker"):
        raw = _load_cached_daily(ticker, CACHE, AS_OF)
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
            vol_base = float(fa["volume"].iloc[-21:-1].mean()) or 1e-9
            rows.append({
                "ticker": ticker, "opened_date": od,
                "extension_atr": ctx.extension_atr,
                "atr_pct": atr / ctx.trigger_close if ctx.trigger_close else 0.0,
                "pullback_bars": ctx.pullback_bars,
                "depth_to_value": (float(last["ema_fast"]) - ctx.swing_low) / atr,
                "rvol": float(last["volume"]) / vol_base,
                "body_frac": float(last["body_frac"]),
                "trend_slope": (float(last["ema_fast"]) - float(last["ema_slow"])) / atr,
                "rsi": ctx.rsi,
            })
    return pd.DataFrame(rows)


def keys(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        "legacy_score": df["signal_score"],
        "inverse_score": -df["signal_score"],
        "fresh_first": -df["extension_atr"],
        "calm_first": -df["atr_pct"],
        "wild_first": df["atr_pct"],
        "deep_pullback": df["depth_to_value"],
        "long_pullback": df["pullback_bars"].astype(float),
        "rvol": df["rvol"],
        "body_frac": df["body_frac"],
        "trend_slope": df["trend_slope"],
        "rsi_high": df["rsi"],
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


def line(name: str, df: pd.DataFrame) -> str:
    s = summarize([SimpleNamespace(**r) for r in df.to_dict("records")])
    return (f"  {name:<24} exp={s.expectancy_r:+.3f} lo={s.expectancy_ci_low:+.3f} "
            f"win={s.win_rate:.2f} fill={s.fill_rate:.0%} n={s.n_total} "
            f"closed={s.n_closed} clus={s.n_clusters}")


def main() -> None:
    feats: pd.DataFrame | None = None
    for slip in ("0.05", "0.10"):
        book = load_book(slip)
        if feats is None:
            feats = compute_features(book[["ticker", "opened_date"]].drop_duplicates())
            print(f"features for {len(feats)} signals (book {len(book)})", file=sys.stderr)
        df = book.merge(feats, on=["ticker", "opened_date"], how="inner")
        closed_n = int(((df["status"] == "closed") & (df["fill_status"] == "filled")).sum())
        print(f"\n================ slip {slip} (n={len(df)}, closed={closed_n}) ================")

        # free cohort reads: stamped on every trade, never judged
        for col in ("mtf_aligned", "timeframe", "quality_tier"):
            for val, sub in sorted(df.groupby(col), key=lambda x: str(x[0])):
                print(line(f"{col}={val}", sub))

        for name, key in keys(df).items():
            d = df.assign(_k=key)
            closed = d[(d["status"] == "closed") & (d["fill_status"] == "filled")
                       & d["realized_r"].notna()].copy()
            closed["q"] = pd.qcut(closed["_k"].rank(method="first"), 5,
                                  labels=["q1", "q2", "q3", "q4", "q5"])
            ladder = "/".join(f"{closed[closed['q'] == q]['realized_r'].mean():+.3f}"
                              for q in ("q1", "q2", "q3", "q4", "q5"))
            hi = closed[closed["q"] == "q5"]
            lo = closed[closed["q"] != "q5"]
            delta_lo = clustered_two_sample_delta_low(by_ticker(hi), by_ticker(lo))
            hi_exp, hi_lo, hi_n = stats(hi)
            d["_sel"] = False
            for _, day in d.groupby("opened_date"):
                d.loc[day.nlargest(5, "_k").index, "_sel"] = True
            sel = d[d["_sel"]]
            sel_exp, sel_lo, _ = stats(sel)
            pick_val = sel["realized_r"].where(
                (sel["status"] == "closed") & (sel["fill_status"] == "filled"), 0.0).mean()
            dig_delta = clustered_two_sample_delta_low(by_ticker(sel), by_ticker(d[~d["_sel"]]))
            print(f"{name:<15} ladder={ladder}  q5: exp={hi_exp:+.3f} lo={hi_lo:+.3f} "
                  f"n={hi_n} dLo={delta_lo:+.3f} | top5/day: exp={sel_exp:+.3f} "
                  f"lo={sel_lo:+.3f} pick_val={pick_val:+.4f} dLo={dig_delta:+.3f}")


if __name__ == "__main__":
    main()

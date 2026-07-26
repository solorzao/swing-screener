"""Q5: has the conviction_tier ladder (what sizing would consume) ever been certified?

The stamped ladder (premium = high_vol AND is_spring; strong = any single signal;
base = none) omits confirm_lag -- the sole certified orderer on the confirmed book
(2026-07-03 rank sweep) -- and its spring ingredient failed re-validation (2026-07-12).
This script asks, on the pinned 511-name replay book (as-of 20260703, 0.05 ATR
slippage baked into realized_r), whether EITHER ladder separates outcomes well enough
to certify conviction-weighted sizing:

  (A) the STAMPED ladder, read directly off the D_dump shards
      (play_type=="reversal", variant=="default" -- the confirm-window-3 book);
  (B) a LAG-AWARE challenger, from recomputed detector context per booked trade:
      premium' := stamped strength=="confirmed" AND confirm_lag>=2 AND rvol>=1.3
      mid      := non-premium' AND (strength=="confirmed" OR rvol>=1.3)
      base'    := everything else (early flips with rvol<1.3)
      (mid is pre-registered exactly as above -- "confirmed-or-highvol non-premium'",
      with highvol := rvol>=1.3, the same threshold premium' and the stamped
      reversal_premium_min_rvol use).

Pre-registered family K=4, one-sided Bonferroni alpha/4 (alpha=0.05 one-sided ->
corrected bootstrap percentile 5/4 = 1.25):
  A1 premium  vs rest        A2 premium+strong vs base
  B1 premium' vs rest        B2 premium'+mid   vs base'
DECISION RULE: a ladder is CERTIFIED for conviction-sizing weights iff its
top-tier-vs-rest corrected clustered one-sided lower bound > 0 with n_closed >= 20
and clusters >= 8 in the top tier. The house 2.5th percentile and a stricter
2.5/4 = 0.625th percentile are printed alongside for context; the 1.25 column decides.

A descriptive 0.10-slippage sidebar re-reads the same contrasts off the C_robust
dumps (variant "confirm3" = the same signal set/config as the D_dump default book;
variant "default" = the legacy window-1 book) -- robustness only, not in the family.

    python scripts/replay_tier_ladder.py --cache-root .cache [--limit 25]
        [--features-cache <path.parquet>] [--skip-robust]
"""

from __future__ import annotations

import argparse
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, cast

import numpy as np
import pandas as pd

from swing_screener.analytics.performance import (
    PerformanceSummary,
    clustered_two_sample_delta_low,
    summarize,
)
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import load_cached_daily
from swing_screener.signals.frame import build_frame
from swing_screener.signals.reversal import detect_reversal

if TYPE_CHECKING:
    from swing_screener.db.models import PaperTrade

AS_OF = "20260703"  # the pinned corpus vintage the dumps were built from
# bootstrap lower-bound percentiles: house 2.5 / pre-registered Bonferroni 5/4 / stricter 2.5/4
PCTS = (2.5, 1.25, 0.625)
DECIDING_PCT = 1.25


class _Row:
    """Minimal trade-like object so ``summarize`` can grade a parquet dump row."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, _: str) -> None:
        return None


def _rows(df: pd.DataFrame) -> list["PaperTrade"]:
    out = [_Row(ticker=r.ticker, status=r.status, fill_status=r.fill_status,
                realized_r=None if pd.isna(r.realized_r) else float(r.realized_r))
           for r in df.itertuples()]
    return cast("list[PaperTrade]", out)


def _summary(df: pd.DataFrame) -> PerformanceSummary:
    return summarize(_rows(df))


def _by_ticker(df: pd.DataFrame) -> dict[str, list[float]]:
    closed = df[(df["status"] == "closed") & (df["fill_status"] == "filled")
                & df["realized_r"].notna()]
    out: dict[str, list[float]] = defaultdict(list)
    for t, r in zip(closed["ticker"], closed["realized_r"]):
        out[t].append(float(r))
    return dict(out)


def _ladder_table(df: pd.DataFrame, tier_col: str, order: list[str]) -> None:
    print(f"{'tier':<10}{'n_total':>8}{'n_closed':>9}{'clusters':>9}{'exp R':>8}"
          f"{'ci_low2.5':>10}{'fill%':>7}", flush=True)
    for tier in order:
        s = _summary(df[df[tier_col] == tier])
        print(f"{tier:<10}{s.n_total:>8d}{s.n_closed:>9d}{s.n_clusters:>9d}"
              f"{s.expectancy_r:>+8.3f}{s.expectancy_ci_low:>+10.3f}{s.fill_rate:>7.0%}",
              flush=True)


def _contrast(name: str, hi: pd.DataFrame, lo: pd.DataFrame) -> tuple[
        PerformanceSummary, dict[float, float]]:
    s_hi, s_lo = _summary(hi), _summary(lo)
    bounds = {p: clustered_two_sample_delta_low(_by_ticker(hi), _by_ticker(lo), lower_pct=p)
              for p in PCTS}
    delta = s_hi.expectancy_r - s_lo.expectancy_r
    print(f"{name:<24} hi: exp={s_hi.expectancy_r:+.3f} n={s_hi.n_closed} "
          f"cl={s_hi.n_clusters} | lo: exp={s_lo.expectancy_r:+.3f} n={s_lo.n_closed} | "
          f"delta={delta:+.3f} dLo2.5={bounds[2.5]:+.4f} "
          f"dLo1.25={bounds[1.25]:+.4f} dLo0.625={bounds[0.625]:+.4f}", flush=True)
    return s_hi, bounds


def _load_book(files: list[Path]) -> pd.DataFrame:
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    return df[df["play_type"] == "reversal"].copy()


def compute_features(book: pd.DataFrame, cache_root: Path, limit: int) -> pd.DataFrame:
    """One row per booked (ticker, opened_date): reversal detector context at the
    signal bar (the frame strictly before the open date), current StrategyConfig --
    which IS the D_dump default book's config (confirm window 3, slippage aside)."""
    cfg = StrategyConfig()
    pairs = book.loc[book["opened_date"].notna(), ["ticker", "opened_date"]].drop_duplicates()
    tickers = sorted(pairs["ticker"].unique())
    if limit:
        tickers = tickers[:limit]
    rows: list[dict[str, object]] = []
    t0 = time.time()
    for i, ticker in enumerate(tickers, 1):
        raw = load_cached_daily(ticker, cache_root, AS_OF)
        if raw is None:
            continue
        f = build_frame(raw, cfg)
        dates = f.index.date
        for od in sorted(set(pairs.loc[pairs["ticker"] == ticker, "opened_date"])):
            od_d = date.fromisoformat(str(od)[:10])
            fa = f[dates < od_d]
            if len(fa) < 60:
                continue
            ctx = detect_reversal(fa, cfg)
            if ctx is None:
                continue
            rows.append({"ticker": ticker, "opened_date": od, "rc_strength": ctx.strength,
                         "rc_lag": ctx.confirm_lag, "rc_rvol": ctx.volume_ratio,
                         "rc_spring": ctx.is_spring})
        if i % 25 == 0 or i == len(tickers):
            print(f"  features: {i}/{len(tickers)} tickers, {len(rows)} contexts, "
                  f"{time.time() - t0:.0f}s", flush=True)
    return pd.DataFrame(rows)


def challenger_tiers(merged: pd.DataFrame) -> pd.Series:
    """The pre-registered lag-aware ladder (see module docstring; stated before compute)."""
    prem = ((merged["strength"] == "confirmed") & (merged["rc_lag"] >= 2)
            & (merged["rc_rvol"] >= 1.3))
    mid = ~prem & ((merged["strength"] == "confirmed") | (merged["rc_rvol"] >= 1.3))
    return pd.Series(np.where(prem, "premium'", np.where(mid, "mid", "base'")),
                     index=merged.index)


def _verdict(tag: str, s_hi: PerformanceSummary, bounds: dict[float, float]) -> bool:
    ok = (bounds[DECIDING_PCT] > 0 and s_hi.n_closed >= 20 and s_hi.n_clusters >= 8)
    print(f"  {tag}: dLo@{DECIDING_PCT}={bounds[DECIDING_PCT]:+.4f}, "
          f"top n_closed={s_hi.n_closed}, clusters={s_hi.n_clusters} -> "
          f"{'CERTIFIED' if ok else 'NOT certified'}", flush=True)
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-root", type=Path, default=Path(".cache"))
    ap.add_argument("--limit", type=int, default=0,
                    help="feature pass: only the first N tickers (0 = all; smoke tests)")
    ap.add_argument("--features-cache", type=Path, default=None,
                    help="load features from this parquet if it exists, else write it")
    ap.add_argument("--skip-robust", action="store_true",
                    help="skip the descriptive 0.10-slippage C_robust sidebar")
    args = ap.parse_args()

    d_files = sorted((args.cache_root / "queue_experiments").glob("D_dump_s*_slip0.05.parquet"))
    book = _load_book(d_files)
    book = book[book["variant"] == "default"].copy()
    print(f"D_dump reversal/default book: {len(d_files)} shards, {len(book)} rows, "
          f"{book['ticker'].nunique()} tickers (pinned as-of {AS_OF}, slip 0.05 baked in)",
          flush=True)

    print("\n=== (A) STAMPED ladder (conviction_tier as booked: premium = high_vol AND "
          "spring) ===", flush=True)
    _ladder_table(book, "conviction_tier", ["premium", "strong", "base"])
    a1 = _contrast("A1 premium vs rest", book[book["conviction_tier"] == "premium"],
                   book[book["conviction_tier"] != "premium"])
    a2 = _contrast("A2 premium+strong vs base",
                   book[book["conviction_tier"].isin(["premium", "strong"])],
                   book[book["conviction_tier"] == "base"])

    print("\n=== (B) LAG-AWARE challenger ladder (recomputed detector context) ===",
          flush=True)
    if args.features_cache is not None and args.features_cache.exists():
        feats = pd.read_parquet(args.features_cache)
        print(f"features loaded from cache: {len(feats)} contexts", flush=True)
    else:
        feats = compute_features(book, args.cache_root, args.limit)
        if args.features_cache is not None:
            args.features_cache.parent.mkdir(parents=True, exist_ok=True)
            feats.to_parquet(args.features_cache)
            print(f"features cached -> {args.features_cache}", flush=True)
    merged = book.merge(feats, on=["ticker", "opened_date"], how="inner")
    sub = book[book["ticker"].isin(feats["ticker"].unique())] if args.limit else book
    agree = (merged["strength"] == merged["rc_strength"]).mean() if len(merged) else 0.0
    print(f"coverage: {len(merged)}/{len(sub)} book rows matched a recomputed context "
          f"({len(merged) / max(len(sub), 1):.1%}); stamped-vs-recomputed strength "
          f"agreement {agree:.1%}", flush=True)
    merged["ctier"] = challenger_tiers(merged)
    _ladder_table(merged, "ctier", ["premium'", "mid", "base'"])
    b1 = _contrast("B1 premium' vs rest", merged[merged["ctier"] == "premium'"],
                   merged[merged["ctier"] != "premium'"])
    b2 = _contrast("B2 premium'+mid vs base'", merged[merged["ctier"] != "base'"],
                   merged[merged["ctier"] == "base'"])

    if not args.skip_robust:
        print("\n=== descriptive 0.10-slippage sidebar (C_robust; NOT in the K=4 family) "
              "===", flush=True)
        c_files = sorted((args.cache_root / "rotation_entry").glob("C_robust_s*_slip0.10.parquet"))
        cbook = _load_book(c_files)
        for variant in ("confirm3", "default"):
            vb = cbook[cbook["variant"] == variant].copy()
            print(f"\n-- variant {variant} (n={len(vb)}) stamped ladder --", flush=True)
            _ladder_table(vb, "conviction_tier", ["premium", "strong", "base"])
            _contrast(f"[0.10 {variant}] premium vs rest",
                      vb[vb["conviction_tier"] == "premium"],
                      vb[vb["conviction_tier"] != "premium"])
            _contrast(f"[0.10 {variant}] prem+strong vs base",
                      vb[vb["conviction_tier"].isin(["premium", "strong"])],
                      vb[vb["conviction_tier"] == "base"])
        vb = cbook[cbook["variant"] == "confirm3"].copy()
        cm = vb.merge(feats, on=["ticker", "opened_date"], how="inner")
        cm["ctier"] = challenger_tiers(cm)
        print(f"\n-- variant confirm3 challenger ladder (same signal set as D_dump "
              f"default; coverage {len(cm)}/{len(vb)}) --", flush=True)
        _ladder_table(cm, "ctier", ["premium'", "mid", "base'"])
        _contrast("[0.10] premium' vs rest", cm[cm["ctier"] == "premium'"],
                  cm[cm["ctier"] != "premium'"])
        _contrast("[0.10] prem'+mid vs base'", cm[cm["ctier"] != "base'"],
                  cm[cm["ctier"] == "base'"])

    print("\n=== DECISION (pre-registered rule; K=4 one-sided Bonferroni, "
          f"deciding bound = clustered {DECIDING_PCT}th pct; top tier needs "
          "n_closed>=20 & clusters>=8) ===", flush=True)
    a_cert = _verdict("(A) stamped ladder   [A1]", a1[0], a1[1])
    b_cert = _verdict("(B) challenger ladder [B1]", b1[0], b1[1])
    for tag, (s, bounds) in (("A2 (context)", a2), ("B2 (context)", b2)):
        print(f"  {tag}: dLo@{DECIDING_PCT}={bounds[DECIDING_PCT]:+.4f} "
              f"(n={s.n_closed}, cl={s.n_clusters})", flush=True)
    if not a_cert and not b_cert:
        print("\nNEITHER ladder certifies: conviction-weighted sizing (Q8) stays BLOCKED; "
              "conviction_tier demotes to a surfacing label.", flush=True)
    else:
        winner = "(A) stamped" if a_cert else "(B) challenger"
        both = " and (B) challenger" if (a_cert and b_cert) else ""
        print(f"\nCERTIFIED: {winner}{both} -- the certified ladder may feed "
              "conviction-sizing weights.", flush=True)


if __name__ == "__main__":
    main()

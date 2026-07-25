"""Q4 rev_rotation_cluster_rs: can a sector-RELATIVE rotation tag discriminate?

The legacy rotation notion (same-sector same-flip-day cluster_size >= 4) tags ~85% of
the reversal book -- a non-discriminating label. This diagnostic re-derives the flip day
for every booked reversal signal (booking lags the flip by 1-3 bars under
reversal_confirm_window=3; the flip is NOT stamped in the dumps) via a signal-only
detector re-pass, joins sectors (fail-open, mirroring pipeline/diversity.cap_by_sector),
and grades ONE pre-registered binary cut fixed before looking:

    rotation_tagged := cluster_size >= 4
                       AND (cluster mean member flip-day close-to-close return
                            - SPY same-day close-to-close return) >= +1.0%

Grading (family = 1 pre-registered contrast; Bonferroni over K=1 is the identity):
  (1) discrimination -- tagged share of the reversal book < 50%;
  (2) edge -- clustered_two_sample_delta_low (ticker clusters) of realized_r
      tagged-vs-untagged on closed filled rows > 0 at slip 0.05, with
      n_closed >= 20 and >= 8 distinct tickers on the tagged side;
  (3) robustness -- direction (mean tagged - mean untagged > 0) holds on the
      C_robust slip-0.10 book, variant=="default".
Outcomes: fail (1) -> retire the rotation thread; pass (1) fail (2) -> falsified hunch,
retire; pass all -> promote as a ranking/attribution dimension ONLY (never a gate).
Excess-return bands x cluster_size are reported DESCRIPTIVELY; only the cut is graded.

    python scripts/replay_rotation_tag.py --cache-root .cache --as-of 20260703
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from swing_screener.analytics.performance import (
    clustered_two_sample_delta_low,
    summarize,
)
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import load_cached_daily
from swing_screener.signals.frame import build_frame
from swing_screener.signals.reversal import detect_reversal

CFG = StrategyConfig()  # reversal_confirm_window=3 on this branch == the D_dump book's config

# Pre-registered cut -- fixed before looking at any outcome. Not CLI knobs on purpose.
MIN_CLUSTER = 4       # legacy breadth threshold (distinct same-sector tickers per flip day)
MIN_EXCESS = 0.01     # cluster mean flip-day return must beat SPY's same-day by >= +1.0%

FlipInfo = tuple[date, int, str, float]  # flip_date, confirm_lag, strength, flip-day return


def load_shards(dump_dir: Path, pattern: str, variant: str) -> pd.DataFrame:
    files = sorted(dump_dir.glob(pattern))
    if not files:
        raise SystemExit(f"no shard dumps matching {pattern} in {dump_dir}")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df = df[(df["play_type"] == "reversal") & (df["variant"] == variant)].copy()
    print(f"loaded {pattern}: {len(files)} shards, {len(df)} reversal rows "
          f"(variant={variant}), {df['ticker'].nunique()} tickers", flush=True)
    dups = int(df.duplicated(["ticker", "opened_date"]).sum())
    if dups:
        print(f"  WARNING: {dups} duplicate (ticker, opened_date) rows", flush=True)
    return df


def load_sectors(cache_root: Path, as_of: str) -> dict[str, str]:
    """ticker -> sector from <cache>/sector/{T}_{date}.json, newest snapshot <= as_of.

    Missing file / missing key = no sector: the signal joins NO cluster and can never be
    tagged (fail-open on the tag side, mirroring cap_by_sector's never-capped unknowns).
    """
    by_ticker: dict[str, tuple[str, str]] = {}
    for f in sorted((cache_root / "sector").glob("*_*.json")):
        ticker, stamp = f.stem.rsplit("_", 1)
        if stamp > as_of:
            continue
        prev = by_ticker.get(ticker)
        if prev is None or stamp > prev[0]:
            sector = json.loads(f.read_text()).get("sector")
            if sector:
                by_ticker[ticker] = (stamp, sector)
    return {t: s for t, (_, s) in by_ticker.items()}


def derive_flips(
    pairs_by_ticker: dict[str, set[date]], cache_root: Path, as_of: str,
    progress_every: int = 25,
) -> tuple[dict[tuple[str, date], FlipInfo], int]:
    """Signal-only detector re-pass: recover the HA flip date for each booked signal.

    ``opened_date`` is the booking day; the detection frame is every bar strictly before
    it (the trigger bar is the last of those -- the replay walk's ``prior``), exactly the
    rank-sweep recompute pattern. The flip bar sits ``confirm_lag`` bars before the
    trigger (0 for early), so flip position = pos(opened_date) - 1 - confirm_lag. The
    flip-day close-to-close return is computed off the same pinned frame.
    """
    flips: dict[tuple[str, date], FlipInfo] = {}
    n_nodetect = 0
    tickers = sorted(pairs_by_ticker)
    t0 = time.time()
    for i, ticker in enumerate(tickers, 1):
        raw = load_cached_daily(ticker, cache_root, as_of)
        if raw is None or len(raw) < 60:
            n_nodetect += len(pairs_by_ticker[ticker])
            continue
        f = build_frame(raw, CFG)
        close = f["close"].to_numpy(dtype=float)
        idx = f.index
        for od in sorted(pairs_by_ticker[ticker]):
            pos = int(idx.searchsorted(pd.Timestamp(od)))  # first bar >= od
            if pos < 60:
                n_nodetect += 1
                continue
            ctx = detect_reversal(f.iloc[:pos], CFG)
            if ctx is None:
                n_nodetect += 1
                continue
            lag = ctx.confirm_lag if ctx.strength == "confirmed" else 0
            fpos = pos - 1 - lag
            ret = close[fpos] / close[fpos - 1] - 1.0 if fpos >= 1 else float("nan")
            flips[(ticker, od)] = (idx[fpos].date(), lag, ctx.strength, ret)
        if i % progress_every == 0 or i == len(tickers):
            print(f"  flip re-pass: {i}/{len(tickers)} tickers, {len(flips)} matched, "
                  f"{n_nodetect} unmatched, {time.time() - t0:.0f}s", flush=True)
    return flips, n_nodetect


def tag_book(
    book: pd.DataFrame, flips: dict[tuple[str, date], FlipInfo],
    sectors: dict[str, str], spy_ret: dict[date, float],
) -> pd.DataFrame:
    """Stamp flip_date / cluster_size / excess / legacy_tag / rotation_tag onto ``book``.

    Clusters are counted over THIS book's full signal universe (missed rows included, so
    per-day per-sector counts cover the whole 511-name corpus): one member per distinct
    (sector, flip_date, ticker). Signals with no sector or no derived flip join no
    cluster (cluster_size 1, never tagged)."""
    out = book.copy()
    keys = list(zip(out["ticker"], out["opened_date"]))
    out["flip_date"] = [flips[k][0] if k in flips else None for k in keys]
    out["flip_ret"] = [flips[k][3] if k in flips else np.nan for k in keys]
    out["derived_strength"] = [flips[k][2] if k in flips else None for k in keys]
    out["sector"] = out["ticker"].map(sectors)

    members = (out.loc[out["flip_date"].notna() & out["sector"].notna(),
                       ["sector", "flip_date", "ticker", "flip_ret"]]
               .drop_duplicates(["sector", "flip_date", "ticker"]))
    cl = (members.groupby(["sector", "flip_date"])
          .agg(cluster_size=("ticker", "nunique"), mean_ret=("flip_ret", "mean"))
          .reset_index())
    cl["excess"] = cl["mean_ret"] - cl["flip_date"].map(spy_ret)

    out = out.merge(cl[["sector", "flip_date", "cluster_size", "excess"]],
                    on=["sector", "flip_date"], how="left")
    out["cluster_size"] = out["cluster_size"].fillna(1).astype(int)
    out["legacy_tag"] = out["cluster_size"] >= MIN_CLUSTER
    out["rotation_tag"] = out["legacy_tag"] & (out["excess"] >= MIN_EXCESS)  # NaN -> False
    return out


def closed_fills(book: pd.DataFrame) -> pd.DataFrame:
    return book[(book["status"] == "closed") & (book["fill_status"] == "filled")
                & book["realized_r"].notna()]


def by_ticker(df: pd.DataFrame) -> dict[str, list[float]]:
    d: dict[str, list[float]] = defaultdict(list)
    for t, r in zip(df["ticker"], df["realized_r"]):
        d[t].append(float(r))
    return d


def side_stats(df: pd.DataFrame) -> tuple[float, float, int, int]:
    s = summarize([SimpleNamespace(**r) for r in df.to_dict("records")])
    return s.expectancy_r, s.expectancy_ci_low, s.n_closed, s.n_clusters


def grade_delta(book: pd.DataFrame, label: str) -> dict[str, float]:
    """Tagged-vs-untagged realized_r on closed filled rows, ticker-clustered."""
    closed = closed_fills(book)
    tag, untag = closed[closed["rotation_tag"]], closed[~closed["rotation_tag"]]
    d_lo = clustered_two_sample_delta_low(by_ticker(tag), by_ticker(untag))
    point = (tag["realized_r"].mean() - untag["realized_r"].mean()
             if len(tag) and len(untag) else float("nan"))
    te, tl, tn, tc = side_stats(tag)
    ue, ul, un, uc = side_stats(untag)
    print(f"\n[{label}] tagged-vs-untagged on closed fills", flush=True)
    print(f"{'side':<10}{'exp R':>8}{'ci_low':>9}{'n_closed':>9}{'clusters':>9}", flush=True)
    print(f"{'tagged':<10}{te:>+8.3f}{tl:>+9.3f}{tn:>9d}{tc:>9d}", flush=True)
    print(f"{'untagged':<10}{ue:>+8.3f}{ul:>+9.3f}{un:>9d}{uc:>9d}", flush=True)
    print(f"delta (tagged - untagged): point {point:+.3f}, "
          f"clustered 95% lower bound {d_lo:+.3f}", flush=True)
    return {"delta_low": d_lo, "point": point, "n_tagged": tn, "clusters_tagged": tc}


def bands_table(book: pd.DataFrame) -> None:
    """Descriptive only: excess bands x cluster_size buckets. Not graded."""
    b = book.copy()
    b["size_bucket"] = pd.cut(b["cluster_size"], [0.5, 1.5, 3.5, 6.5, 9.5, np.inf],
                              labels=["1", "2-3", "4-6", "7-9", "10+"])
    edges = [-np.inf, 0.0, 0.01, 0.02, np.inf]
    b["excess_band"] = pd.cut(b["excess"], edges, right=False,
                              labels=["<0", "0-1%", "1-2%", ">=2%"])
    print("\n[descriptive] excess-vs-SPY bands x cluster_size "
          "(rows | closed fills | mean R):", flush=True)
    hdr = f"{'size':<6}" + "".join(f"{s:>22}" for s in ("<0", "0-1%", "1-2%", ">=2%", "no-excess"))
    print(hdr, flush=True)
    for sb in ("1", "2-3", "4-6", "7-9", "10+"):
        row = b[b["size_bucket"] == sb]
        cells = []
        for eb in ("<0", "0-1%", "1-2%", ">=2%", None):
            cell = row[row["excess_band"].isna()] if eb is None else row[row["excess_band"] == eb]
            c = closed_fills(cell)
            mean_r = f"{c['realized_r'].mean():+.3f}" if len(c) else "  --  "
            cells.append(f"{len(cell):>7}|{len(c):>6}|{mean_r:>7}")
        print(f"{sb:<6}" + "".join(f"{c:>22}" for c in cells), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-root", type=Path, default=Path(".cache"))
    ap.add_argument("--as-of", default="20260703", metavar="YYYYMMDD",
                    help="pinned corpus vintage (dumps + sector + daily frames)")
    ap.add_argument("--progress-every", type=int, default=25)
    args = ap.parse_args()
    cache = args.cache_root

    sectors = load_sectors(cache, args.as_of)
    print(f"sector map: {len(sectors)} tickers", flush=True)
    spy = load_cached_daily("SPY", cache, args.as_of)
    if spy is None:
        raise SystemExit("no cached SPY daily frame")
    spy_ret = dict(zip([d.date() for d in spy.index], spy["close"].pct_change()))

    book05 = load_shards(cache / "queue_experiments", "D_dump_s*_slip0.05.parquet", "default")
    book10 = load_shards(cache / "rotation_entry", "C_robust_s*_slip0.10.parquet", "default")
    book10c3 = load_shards(cache / "rotation_entry", "C_robust_s*_slip0.10.parquet", "confirm3")

    pairs: dict[str, set[date]] = defaultdict(set)
    for b in (book05, book10, book10c3):
        for t, od in zip(b["ticker"], b["opened_date"]):
            pairs[t].add(od)
    n_pairs = sum(len(v) for v in pairs.values())
    print(f"\nflip re-pass over {len(pairs)} tickers, {n_pairs} unique (ticker, opened_date) "
          f"pairs (union of the three books) ...", flush=True)
    flips, n_nodetect = derive_flips(pairs, cache, args.as_of, args.progress_every)
    print(f"derived flips for {len(flips)}/{n_pairs} pairs "
          f"({n_nodetect} unmatched -- excluded from clusters, never tagged)", flush=True)

    tagged05 = tag_book(book05, flips, sectors, spy_ret)
    matched = tagged05["derived_strength"].notna()
    agree = (tagged05.loc[matched, "derived_strength"]
             == tagged05.loc[matched, "strength"]).mean()
    print(f"strength agreement (dump vs re-derived, D_dump): {agree:.1%} "
          f"on {int(matched.sum())} matched rows", flush=True)
    no_sector = (~tagged05["ticker"].isin(sectors)).mean()
    print(f"no-sector share of D_dump rows: {no_sector:.1%}", flush=True)

    # --- SANITY: reproduce the legacy tag (cluster_size >= 4, no return condition) ---
    legacy_share = tagged05["legacy_tag"].mean()
    legacy_share_closed = closed_fills(tagged05)["legacy_tag"].mean()
    print(f"\n[sanity] LEGACY tag (cluster_size >= {MIN_CLUSTER}) share of the reversal "
          f"book: {legacy_share:.1%} of all rows, {legacy_share_closed:.1%} of closed "
          f"fills (expected ~85%)", flush=True)

    # --- (1) discrimination ---
    rot_share = tagged05["rotation_tag"].mean()
    rot_share_closed = closed_fills(tagged05)["rotation_tag"].mean()
    print(f"\n[grade 1] ROTATION tag (cluster_size >= {MIN_CLUSTER} AND excess >= "
          f"{MIN_EXCESS:+.1%}) share: {rot_share:.1%} of all rows, "
          f"{rot_share_closed:.1%} of closed fills -- bar: < 50%", flush=True)

    # --- (2) edge at slip 0.05 ---
    g05 = grade_delta(tagged05, "grade 2: D_dump slip 0.05")
    bands_table(tagged05)

    # --- (3) robustness at slip 0.10 (pre-registered book: C_robust variant=default) ---
    tagged10 = tag_book(book10, flips, sectors, spy_ret)
    g10 = grade_delta(tagged10, "grade 3: C_robust slip 0.10, variant=default")
    tagged10c3 = tag_book(book10c3, flips, sectors, spy_ret)
    grade_delta(tagged10c3, "supplementary (NOT graded): C_robust slip 0.10, "
                            "variant=confirm3 (same signal set as D_dump)")

    # --- verdict per the pre-registered rule ---
    pass1 = rot_share < 0.50
    pass2 = (g05["delta_low"] > 0 and g05["n_tagged"] >= 20 and g05["clusters_tagged"] >= 8)
    pass3 = g10["point"] > 0
    print("\n================ PRE-REGISTERED VERDICT ================", flush=True)
    print(f"(1) discrimination: tagged share {rot_share:.1%} < 50% -> "
          f"{'PASS' if pass1 else 'FAIL'}", flush=True)
    print(f"(2) edge @0.05: delta_low {g05['delta_low']:+.3f} > 0, n_tagged "
          f"{g05['n_tagged']} >= 20, clusters {g05['clusters_tagged']} >= 8 -> "
          f"{'PASS' if pass2 else 'FAIL'}", flush=True)
    print(f"(3) direction @0.10 (default book): point delta {g10['point']:+.3f} > 0 -> "
          f"{'PASS' if pass3 else 'FAIL'} (lower bound {g10['delta_low']:+.3f})", flush=True)
    if not pass1:
        print("OUTCOME: fail (1) -> RETIRE the rotation thread for good.", flush=True)
    elif not pass2:
        print("OUTCOME: pass (1), fail (2) -> record falsified hunch, RETIRE the "
              "rotation thread.", flush=True)
    elif pass3:
        print("OUTCOME: pass (1)+(2)+(3) -> PROMOTE as a ranking/attribution dimension "
              "ONLY (pre-committed: no gate under any outcome).", flush=True)
    else:
        print("OUTCOME: pass (1)+(2) but direction fails at 0.10 -> NOT robust to cost; "
              "record falsified hunch, RETIRE the rotation thread.", flush=True)


if __name__ == "__main__":
    main()

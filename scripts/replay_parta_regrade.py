"""Part-A re-grade: the four recorded Part-A diagnostics, re-run on the CURRENT-target
reversal book, side-by-side with their recorded legacy-book values.

The pinned D_dump shards (2026-07-03) predate the reversal_retrace_frac 0.786 -> 1.0
default flip: same signals/entries/stops/fills, LEGACY targets -- realized_r differs on
~10.7k of the 70,277 reversal rows vs the current default. The ``variant == "default"``
rows of the R_stop_a shards are the SAME corpus/config under the CURRENT default target
rule (verified deterministic-identical to the C_ceil_lo default reversal book). This
script re-runs the KEY cells of the four already-recorded diagnostics on that current
book, using the house seams only (summarize / breakdown-equivalent / _clustered_ci_low /
clustered_two_sample_delta_low, cluster unit = ticker, shards loaded in sorted order so
every bootstrap is bit-reproducible):

  Q1  rev_bear_highvol_crosstab key cells      (mirrors replay_rev_crosstab.py)
  Q2  early x high cell A + pre-registered bar (mirrors replay_early_highvol.py)
  Q4  rotation tagged-vs-untagged delta        (mirrors replay_rotation_tag.py; the tag
      is entry-side -- flip-day detector re-pass, vintage-invariant -- re-derived here)
  Q5  stamped conviction-ladder contrasts A1/A2 (+ lag-aware B1, cheap off the same
      detector re-pass)                        (mirrors replay_tier_ladder.py)

Each recomputation is printed THREE ways: the recorded legacy value (from the diagnostic
write-ups), the same cell recomputed off the legacy book (replication check), and the
cell on the current book (the re-grade). Headline book-agreement stats quantify the
vintage effect for the addendum. Read-only over the cache; writes markdown to --out.

    PYTHONPATH=src python scripts/replay_parta_regrade.py \
        --cache-root <repo>/.cache [--repass-cache ctx.parquet] [--limit N] \
        [--out results_regrade.md]
"""

from __future__ import annotations

import argparse
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from swing_screener.analytics.performance import (
    PerformanceSummary,
    _clustered_ci_low,
    closed_by_ticker,
    clustered_two_sample_delta_low,
    summarize,
)
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import load_cached_daily
from swing_screener.signals.frame import build_frame
from swing_screener.signals.reversal import detect_reversal

CFG = StrategyConfig()  # reversal_confirm_window=3 == both books' signal-side config

# --- Q1 (crosstab) constants: house lb = clustered 2.5th pct; Bonferroni over K=6 cells
_K_CELLS = 6
_CORR_PCT = 2.5 / _K_CELLS
_Z_CORR = NormalDist().inv_cdf(1 - _CORR_PCT / 100.0)
_ELIGIBLE_N, _ELIGIBLE_CL = 20, 8

# --- Q2 (early x high) pre-registered bar
_HW_BAR = 0.10

# --- Q4 (rotation) pre-registered cut -- fixed before looking; identical to the original
MIN_CLUSTER = 4
MIN_EXCESS = 0.01

# --- Q5 (tier ladder) bootstrap percentiles; the 1.25 (= 5/4, K=4 one-sided) decides
PCTS = (2.5, 1.25, 0.625)
DECIDING_PCT = 1.25

RECORDED = {
    "q1_bearxhigh": "exp +0.299  lb +0.218  lb_bonf6 +0.187  n=1225",
    "q1_bearxnothigh": "lb +0.080",
    "q1_bullxhigh": "lb -0.055",
    "q1_conf_bearxhigh": "raw lb +0.057  lb_bonf6 -0.003",
    "q2_cellA": "exp +0.171  lb +0.109  n=1940  clusters=219  hw_low 0.063",
    "q4_delta": "point +0.0004  clustered lb -0.029",
    "q5_a1": "delta +0.050  dLo1.25 -0.043",
    "q5_a2": "point -0.0150",
}

_MD: list[str] = []


def emit(line: str = "") -> None:
    print(line, flush=True)
    _MD.append(line)


class _Row:
    """Minimal trade-like object so the house ``summarize`` can grade a dump row."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, _: str) -> None:
        return None


def _rows(df: pd.DataFrame) -> list:
    return [
        _Row(
            ticker=r.ticker,
            status=r.status,
            fill_status=r.fill_status,
            realized_r=None if pd.isna(r.realized_r) else float(r.realized_r),
        )
        for r in df.itertuples()
    ]


def _summary(df: pd.DataFrame) -> PerformanceSummary:
    return summarize(_rows(df))


def _cell(df: pd.DataFrame, *, corrected: bool = False) -> tuple[PerformanceSummary, float | None]:
    """House summary of one cohort; optionally the K=6 Bonferroni-corrected lower bound
    (same construction as replay_rev_crosstab.py: clustered bootstrap at the alpha/6
    percentile, floored by the equally-corrected IID bound via min() inside the seam)."""
    rows = _rows(df)
    s = summarize(rows)
    lb6: float | None = None
    if corrected and s.n_closed:
        by_t = closed_by_ticker(rows)
        iid_corr = s.expectancy_r - _Z_CORR * s.expectancy_stderr
        lb6, _, _ = _clustered_ci_low(by_t, iid_corr, lower_pct=_CORR_PCT)
    return s, lb6


def _clears(lb: float, n: int, cl: int) -> bool:
    return lb > 0 and n >= _ELIGIBLE_N and cl >= _ELIGIBLE_CL


def load_books(cache_root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    qdir = cache_root / "queue_experiments"
    d_files = sorted(qdir.glob("D_dump_s*_slip0.05.parquet"))
    r_files = sorted(qdir.glob("R_stop_a_s*_slip0.05.parquet"))
    if not d_files or not r_files:
        raise SystemExit(f"missing shards under {qdir} (D_dump: {len(d_files)}, "
                         f"R_stop_a: {len(r_files)})")
    d = pd.concat([pd.read_parquet(f) for f in d_files], ignore_index=True)
    r = pd.concat([pd.read_parquet(f) for f in r_files], ignore_index=True)
    legacy = d[d["play_type"] == "reversal"].copy()
    current = r[(r["play_type"] == "reversal") & (r["variant"] == "default")].copy()
    emit(f"- legacy  book: {len(d_files)} D_dump shards   -> {len(legacy)} reversal rows, "
         f"{legacy['ticker'].nunique()} tickers (all variant=default)")
    emit(f"- current book: {len(r_files)} R_stop_a shards -> {len(current)} reversal rows "
         f"(variant=default), {current['ticker'].nunique()} tickers")
    return legacy, current


def agreement_section(legacy: pd.DataFrame, current: pd.DataFrame) -> None:
    emit()
    emit("## Vintage effect: book agreement")
    emit()
    m = legacy.merge(current, on=["ticker", "opened_date"], suffixes=("_l", "_c"))
    emit("```")
    emit(f"matched (ticker, opened_date) pairs: {len(m)} "
         f"(legacy {len(legacy)}, current {len(current)})")
    for col in ("entry_price", "stop", "entry_date", "exit_reason", "strength"):
        a, b = m[f"{col}_l"], m[f"{col}_c"]
        diff = int((~((a == b) | (a.isna() & b.isna()))).sum())
        emit(f"  {col:<12} differing rows: {diff}")
    tgt_diff = int((~np.isclose(m["target_l"], m["target_c"], equal_nan=True)).sum())
    emit(f"  {'target':<12} differing rows: {tgt_diff}  (the retrace-frac flip)")
    same_r = (m["realized_r_l"] == m["realized_r_c"]) | (
        m["realized_r_l"].isna() & m["realized_r_c"].isna())
    emit(f"  realized_r   identical rows: {int(same_r.sum())} ({same_r.mean():.2%}); "
         f"differing {int((~same_r).sum())} ({(~same_r).mean():.2%})")

    def _cf(df: pd.DataFrame, suf: str) -> pd.Series:
        return ((df[f"status{suf}"] == "closed") & (df[f"fill_status{suf}"] == "filled")
                & df[f"realized_r{suf}"].notna())

    cl_l, cl_c = _cf(m, "_l"), _cf(m, "_c")
    emit(f"  closed fills: legacy {int(cl_l.sum())}, current {int(cl_c.sum())}, "
         f"both {int((cl_l & cl_c).sum())}")
    mean_l = m.loc[cl_l, "realized_r_l"].mean()
    mean_c = m.loc[cl_c, "realized_r_c"].mean()
    emit(f"  mean R (own closed fills): legacy {mean_l:+.4f}, current {mean_c:+.4f}, "
         f"shift {mean_c - mean_l:+.4f}")
    both = cl_l & cl_c
    pair_shift = (m.loc[both, "realized_r_c"] - m.loc[both, "realized_r_l"]).mean()
    emit(f"  paired mean R shift (rows closed+filled in BOTH): {pair_shift:+.4f} "
         f"on n={int(both.sum())}")
    for s in ("early", "confirmed"):
        sm = m["strength_l"] == s
        ml = m.loc[cl_l & sm, "realized_r_l"].mean()
        mc = m.loc[cl_c & sm, "realized_r_c"].mean()
        ps = (m.loc[both & sm, "realized_r_c"] - m.loc[both & sm, "realized_r_l"]).mean()
        emit(f"    strength={s:<10} mean R legacy {ml:+.4f} -> current {mc:+.4f} "
             f"(shift {mc - ml:+.4f}; paired {ps:+.4f} on n={int((both & sm).sum())})")
    emit("```")


# ---------------------------------------------------------------- Q1: crosstab key cells
def q1_key_cells(rev: pd.DataFrame) -> dict[str, tuple]:
    out: dict[str, tuple] = {}
    for tr in ("bull", "bear"):
        for ti in ("low", "med", "high"):
            s, lb6 = _cell(rev[(rev["market_trend"] == tr) & (rev["volatility_tier"] == ti)],
                           corrected=True)
            out[f"{tr}x{ti}"] = (s, lb6)
    bear = rev[rev["market_trend"] == "bear"]
    out["bearxnot-high"] = _cell(bear[bear["volatility_tier"].isin(["low", "med"])])
    out["trend=bear"] = _cell(rev[rev["market_trend"].astype(str) == "bear"])
    out["tier=high"] = _cell(rev[rev["volatility_tier"] == "high"])
    conf = rev[rev["strength"] == "confirmed"]
    out["conf_bearxhigh"] = _cell(
        conf[(conf["market_trend"] == "bear") & (conf["volatility_tier"] == "high")],
        corrected=True)
    return out


def q1_line(label: str, res: tuple[PerformanceSummary, float | None]) -> str:
    s, lb6 = res
    lb6s = f"{lb6:>+9.3f}" if lb6 is not None else f"{'-':>9}"
    return (f"{label:<26}{s.expectancy_r:>+8.3f}{s.expectancy_ci_low:>+9.3f}{lb6s}"
            f"{s.n_closed:>9d}{s.n_clusters:>9d}")


def q1_verdict_clauses(res: dict[str, tuple]) -> tuple[bool, list[str]]:
    bh_s, bh6 = res["bearxhigh"]
    bnh_s, _ = res["bearxnot-high"]
    gh_s, _ = res["bullxhigh"]
    cands = {k: res[k][0] for k in ("trend=bear", "tier=high", "bearxhigh")}
    clearing = [k for k, s in cands.items()
                if _clears(s.expectancy_ci_low, s.n_closed, s.n_clusters)]
    strongest = (max(clearing, key=lambda k: cands[k].expectancy_ci_low)
                 if clearing else "none")
    clauses = [
        ("bear is the load-bearing axis (bear x high clears the house bar; "
         "bull x high does not)",
         _clears(bh_s.expectancy_ci_low, bh_s.n_closed, bh_s.n_clusters)
         and not _clears(gh_s.expectancy_ci_low, gh_s.n_closed, gh_s.n_clusters)),
        ("high-vol amplifies inside bear (bear x high exp AND lb above bear x not-high)",
         bh_s.expectancy_r > bnh_s.expectancy_r
         and bh_s.expectancy_ci_low > bnh_s.expectancy_ci_low),
        ("bull x high fails (house lb <= 0)", gh_s.expectancy_ci_low <= 0),
        ("strongest cohort = bear x high (by house lb among clearing cohorts)",
         strongest == "bearxhigh"),
        ("bear x high survives its own Bonferroni-6 bound (lb_bonf6 > 0)",
         bh6 is not None and bh6 > 0),
    ]
    lines = [f"  {'PASS' if ok else 'FAIL'}  {label}" for label, ok in clauses]
    lines.append(f"  strongest single cohort: {strongest}")
    return all(ok for _, ok in clauses), lines


def run_q1(legacy: pd.DataFrame, current: pd.DataFrame) -> bool:
    emit()
    emit("## Q1 -- rev_bear_highvol_crosstab key cells (closed fills, "
         "market_trend x volatility_tier)")
    emit()
    emit(f"Recorded (legacy book): bear x high {RECORDED['q1_bearxhigh']}; "
         f"bear x not-high pooled {RECORDED['q1_bearxnothigh']}; "
         f"bull x high {RECORDED['q1_bullxhigh']}; "
         f"CONFIRMED bear x high {RECORDED['q1_conf_bearxhigh']}.")
    emit()
    emit("```")
    results = {}
    for tag, rev in (("LEGACY (replication)", legacy), ("CURRENT (re-grade)", current)):
        res = q1_key_cells(rev)
        results[tag] = res
        emit(f"[{tag}]")
        emit(f"{'cohort':<26}{'exp R':>8}{'ci_low':>9}{'lb_bonf6':>9}{'n_closed':>9}"
             f"{'clusters':>9}")
        for tr in ("bull", "bear"):
            for ti in ("low", "med", "high"):
                emit(q1_line(f"{tr} x {ti}", res[f"{tr}x{ti}"]))
        emit(q1_line("bear x not-high (pooled)", res["bearxnot-high"]))
        emit(q1_line("trend=bear (marginal)", res["trend=bear"]))
        emit(q1_line("tier=high (marginal)", res["tier=high"]))
        emit(q1_line("CONFIRMED bear x high", res["conf_bearxhigh"]))
        emit()
    emit("[verdict-sentence check on the CURRENT book]")
    survives, lines = q1_verdict_clauses(results["CURRENT (re-grade)"])
    for ln in lines:
        emit(ln)
    emit("```")
    emit()
    emit(f"**Q1 verdict sentence {'SURVIVES' if survives else 'DOES NOT SURVIVE'} "
         f"on the current target rule.**")
    return survives


# ------------------------------------------------------------------- Q2: early x high A
def run_q2(legacy: pd.DataFrame, current: pd.DataFrame) -> bool:
    emit()
    emit("## Q2 -- early x high cell A (pre-registered bar: lb>0, n>=20, cl>=8, "
         "hw_low<=0.10)")
    emit()
    emit(f"Recorded (legacy book): {RECORDED['q2_cellA']}.")
    emit()
    emit("```")
    passes = False
    for tag, rev in (("LEGACY (replication)", legacy), ("CURRENT (re-grade)", current)):
        emit(f"[{tag}]")
        emit(f"{'cell':<18}{'role':<9}{'exp R':>8}{'ci_low':>9}{'hw_low':>8}"
             f"{'n_closed':>9}{'clusters':>9}")
        primary: PerformanceSummary | None = None
        for strength, vol in (("early", "high"), ("early", "med"), ("early", "low"),
                              ("confirmed", "high"), ("confirmed", "med")):
            cd = rev[(rev["strength"] == strength) & (rev["volatility_tier"] == vol)]
            s = _summary(cd)
            hw = s.expectancy_r - s.expectancy_ci_low
            role = "PRIMARY" if (strength, vol) == ("early", "high") else "control"
            emit(f"{strength + '|' + vol:<18}{role:<9}{s.expectancy_r:>+8.3f}"
                 f"{s.expectancy_ci_low:>+9.3f}{hw:>8.3f}{s.n_closed:>9d}{s.n_clusters:>9d}")
            if role == "PRIMARY":
                primary = s
        assert primary is not None
        hw = primary.expectancy_r - primary.expectancy_ci_low
        checks = (
            ("clustered 95% lb > 0", primary.expectancy_ci_low > 0,
             f"lb = {primary.expectancy_ci_low:+.3f}"),
            ("n_closed >= 20", primary.n_closed >= 20, f"n_closed = {primary.n_closed}"),
            ("clusters >= 8", primary.n_clusters >= 8, f"clusters = {primary.n_clusters}"),
            (f"half-width <= {_HW_BAR:.2f}R", hw <= _HW_BAR, f"hw_low = {hw:.3f}R"),
        )
        for label, ok, detail in checks:
            emit(f"  {'PASS' if ok else 'FAIL'}  {label:<28} {detail}")
        if tag.startswith("CURRENT"):
            passes = all(ok for _, ok, _ in checks)
        emit()
    emit("```")
    emit()
    emit(f"**Q2 cell A pre-registered bar {'STILL PASSES' if passes else 'now FAILS'} "
         f"on the current book.**")
    return passes


# ------------------------------------- detector re-pass (shared by Q4 tag and Q5 B1)
def derive_context(pairs_by_ticker: dict[str, set], cache_root: Path, as_of: str,
                   progress_every: int = 50) -> pd.DataFrame:
    """Signal-only detector re-pass per booked (ticker, opened_date): the HA flip date /
    flip-day return (Q4 rotation tag) plus the detector context fields the Q5 lag-aware
    challenger consumes. Identical walk to replay_rotation_tag.derive_flips /
    replay_tier_ladder.compute_features: the detection frame is every bar strictly
    before the booking day; entries are vintage-invariant so one pass serves both books."""
    recs: list[dict[str, object]] = []
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
            pos = int(idx.searchsorted(pd.Timestamp(od)))  # first bar >= booking day
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
            recs.append({"ticker": ticker, "opened_date": od,
                         "flip_date": idx[fpos].date(), "flip_ret": ret,
                         "rc_strength": ctx.strength, "rc_lag": int(ctx.confirm_lag),
                         "rc_rvol": float(ctx.volume_ratio),
                         "rc_spring": bool(ctx.is_spring)})
        if i % progress_every == 0 or i == len(tickers):
            print(f"  re-pass: {i}/{len(tickers)} tickers, {len(recs)} matched, "
                  f"{n_nodetect} unmatched, {time.time() - t0:.0f}s", flush=True)
    return pd.DataFrame(recs)


def _to_date(s: pd.Series) -> pd.Series:
    if len(s) and not isinstance(s.iloc[0], date):
        return pd.to_datetime(s).dt.date
    return s


def load_sectors(cache_root: Path, as_of: str) -> dict[str, str]:
    """ticker -> sector, newest snapshot <= as_of (same fail-open read as the original)."""
    import json
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


# ------------------------------------------------------------ Q4: rotation tag re-grade
def tag_book(book: pd.DataFrame, ctx: pd.DataFrame, sectors: dict[str, str],
             spy_ret: dict[date, float]) -> pd.DataFrame:
    """Stamp cluster_size / excess / rotation_tag; merge-based but semantically identical
    to replay_rotation_tag.tag_book (left merges preserve book row order, so the
    ticker-cluster insertion order feeding the bootstrap is unchanged)."""
    out = book.merge(ctx[["ticker", "opened_date", "flip_date", "flip_ret"]],
                     on=["ticker", "opened_date"], how="left")
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
    out["rotation_tag"] = (out["cluster_size"] >= MIN_CLUSTER) & (out["excess"] >= MIN_EXCESS)
    return out


def closed_fills(book: pd.DataFrame) -> pd.DataFrame:
    return book[(book["status"] == "closed") & (book["fill_status"] == "filled")
                & book["realized_r"].notna()]


def _by_ticker(df: pd.DataFrame) -> dict[str, list[float]]:
    d: dict[str, list[float]] = defaultdict(list)
    for t, r in zip(closed_fills(df)["ticker"], closed_fills(df)["realized_r"]):
        d[t].append(float(r))
    return dict(d)


def grade_delta(book: pd.DataFrame, label: str) -> dict[str, float]:
    closed = closed_fills(book)
    tag, untag = closed[closed["rotation_tag"]], closed[~closed["rotation_tag"]]
    d_lo = clustered_two_sample_delta_low(_by_ticker(tag), _by_ticker(untag))
    point = (tag["realized_r"].mean() - untag["realized_r"].mean()
             if len(tag) and len(untag) else float("nan"))
    s_t, s_u = _summary(tag), _summary(untag)
    emit(f"[{label}] tagged-vs-untagged on closed fills")
    emit(f"{'side':<10}{'exp R':>8}{'ci_low':>9}{'n_closed':>9}{'clusters':>9}")
    emit(f"{'tagged':<10}{s_t.expectancy_r:>+8.3f}{s_t.expectancy_ci_low:>+9.3f}"
         f"{s_t.n_closed:>9d}{s_t.n_clusters:>9d}")
    emit(f"{'untagged':<10}{s_u.expectancy_r:>+8.3f}{s_u.expectancy_ci_low:>+9.3f}"
         f"{s_u.n_closed:>9d}{s_u.n_clusters:>9d}")
    emit(f"delta (tagged - untagged): point {point:+.4f}, clustered 95% lower bound "
         f"{d_lo:+.4f}")
    return {"delta_low": d_lo, "point": point, "n_tagged": s_t.n_closed,
            "clusters_tagged": s_t.n_clusters}


def run_q4(legacy: pd.DataFrame, current: pd.DataFrame, ctx: pd.DataFrame,
           cache_root: Path, as_of: str, smoke: bool) -> bool:
    emit()
    emit("## Q4 -- rotation tagged-vs-untagged delta (tag re-derived; entry-side, "
         "vintage-invariant)")
    emit()
    emit(f"Recorded (legacy book): {RECORDED['q4_delta']} -> RETIRE.")
    if smoke:
        emit("NOTE: SMOKE run (--limit) -- tag derived on a ticker subset only.")
    emit()
    sectors = load_sectors(cache_root, as_of)
    spy = load_cached_daily("SPY", cache_root, as_of)
    if spy is None:
        raise SystemExit("no cached SPY daily frame")
    spy_ret = dict(zip([d.date() for d in spy.index], spy["close"].pct_change()))
    emit("```")
    emit(f"sector map: {len(sectors)} tickers; derived contexts: {len(ctx)} pairs")
    retire_stands = True
    g: dict[str, float] = {}
    for tag_name, book in (("LEGACY (replication)", legacy), ("CURRENT (re-grade)", current)):
        tb = tag_book(book, ctx, sectors, spy_ret)
        share = tb["rotation_tag"].mean()
        share_cl = closed_fills(tb)["rotation_tag"].mean()
        emit(f"\n[{tag_name}] rotation-tag share: {share:.1%} of all rows, "
             f"{share_cl:.1%} of closed fills (grade 1 bar: < 50%)")
        g = grade_delta(tb, tag_name)
        if tag_name.startswith("CURRENT"):
            pass1 = share < 0.50
            pass2 = (g["delta_low"] > 0 and g["n_tagged"] >= 20
                     and g["clusters_tagged"] >= 8)
            emit(f"\n(1) discrimination: {share:.1%} < 50% -> {'PASS' if pass1 else 'FAIL'}")
            emit(f"(2) edge: delta_low {g['delta_low']:+.4f} > 0, n_tagged "
                 f"{g['n_tagged']:.0f} >= 20, clusters {g['clusters_tagged']:.0f} >= 8 -> "
                 f"{'PASS' if pass2 else 'FAIL'}")
            retire_stands = not (pass1 and pass2)
    emit("```")
    emit()
    if retire_stands:
        emit("**Q4 RETIRE verdict STANDS on the current book.**")
    else:
        emit("**Q4 RETIRE verdict is CHALLENGED on the current book (grades 1+2 pass); "
             "the 0.10-slippage robustness leg would need a re-run before any reversal.**")
    return retire_stands


# --------------------------------------------------------- Q5: conviction-ladder re-grade
def _contrast(name: str, hi: pd.DataFrame, lo: pd.DataFrame) -> tuple[
        PerformanceSummary, dict[float, float], float]:
    s_hi, s_lo = _summary(hi), _summary(lo)
    bounds = {p: clustered_two_sample_delta_low(_by_ticker(hi), _by_ticker(lo), lower_pct=p)
              for p in PCTS}
    delta = s_hi.expectancy_r - s_lo.expectancy_r
    emit(f"{name:<26} hi: exp={s_hi.expectancy_r:+.3f} n={s_hi.n_closed} "
         f"cl={s_hi.n_clusters} | lo: exp={s_lo.expectancy_r:+.3f} n={s_lo.n_closed} | "
         f"delta={delta:+.4f} dLo2.5={bounds[2.5]:+.4f} dLo1.25={bounds[1.25]:+.4f} "
         f"dLo0.625={bounds[0.625]:+.4f}")
    return s_hi, bounds, delta


def challenger_tiers(merged: pd.DataFrame) -> pd.Series:
    prem = ((merged["strength"] == "confirmed") & (merged["rc_lag"] >= 2)
            & (merged["rc_rvol"] >= 1.3))
    mid = ~prem & ((merged["strength"] == "confirmed") | (merged["rc_rvol"] >= 1.3))
    return pd.Series(np.where(prem, "premium'", np.where(mid, "mid", "base'")),
                     index=merged.index)


def run_q5(legacy: pd.DataFrame, current: pd.DataFrame, ctx: pd.DataFrame,
           smoke: bool) -> bool:
    emit()
    emit("## Q5 -- stamped conviction-ladder contrasts (K=4 one-sided Bonferroni; "
         "deciding bound = clustered 1.25th pct)")
    emit()
    emit(f"Recorded (legacy book): A1 premium-vs-rest {RECORDED['q5_a1']}; "
         f"A2 premium+strong-vs-base {RECORDED['q5_a2']} -> NOT CERTIFIED.")
    emit("(Label note: the recorded A2 '-0.0150' reproduces as the A2 clustered dLo2.5 "
         "bound, not the point delta -- the legacy A2 point delta replicates as +0.0115.)")
    if smoke:
        emit("NOTE: SMOKE run (--limit) -- B1 coverage is a ticker subset only.")
    emit()
    emit("```")
    not_certified = True
    for tag_name, book in (("LEGACY (replication)", legacy), ("CURRENT (re-grade)", current)):
        emit(f"[{tag_name}] stamped ladder")
        for tier in ("premium", "strong", "base"):
            s = _summary(book[book["conviction_tier"] == tier])
            emit(f"  {tier:<9}n_total={s.n_total:<7d}n_closed={s.n_closed:<7d}"
                 f"cl={s.n_clusters:<5d}exp={s.expectancy_r:+.3f}  "
                 f"lb2.5={s.expectancy_ci_low:+.3f}")
        a1 = _contrast("A1 premium vs rest", book[book["conviction_tier"] == "premium"],
                       book[book["conviction_tier"] != "premium"])
        _contrast("A2 premium+strong vs base",
                  book[book["conviction_tier"].isin(["premium", "strong"])],
                  book[book["conviction_tier"] == "base"])
        merged = book.merge(ctx[["ticker", "opened_date", "rc_strength", "rc_lag",
                                 "rc_rvol"]], on=["ticker", "opened_date"], how="inner")
        sub = (book[book["ticker"].isin(ctx["ticker"].unique())] if smoke else book)
        emit(f"  B1 coverage: {len(merged)}/{len(sub)} rows matched a recomputed context "
             f"({len(merged) / max(len(sub), 1):.1%})")
        merged = merged.copy()
        merged["ctier"] = challenger_tiers(merged)
        b1 = _contrast("B1 premium' vs rest", merged[merged["ctier"] == "premium'"],
                       merged[merged["ctier"] != "premium'"])
        if tag_name.startswith("CURRENT"):
            a_cert = (a1[1][DECIDING_PCT] > 0 and a1[0].n_closed >= 20
                      and a1[0].n_clusters >= 8)
            b_cert = (b1[1][DECIDING_PCT] > 0 and b1[0].n_closed >= 20
                      and b1[0].n_clusters >= 8)
            emit(f"\n  (A) stamped   [A1]: dLo@{DECIDING_PCT}={a1[1][DECIDING_PCT]:+.4f}, "
                 f"top n={a1[0].n_closed}, cl={a1[0].n_clusters} -> "
                 f"{'CERTIFIED' if a_cert else 'NOT certified'}")
            emit(f"  (B) challenger [B1]: dLo@{DECIDING_PCT}={b1[1][DECIDING_PCT]:+.4f}, "
                 f"top n={b1[0].n_closed}, cl={b1[0].n_clusters} -> "
                 f"{'CERTIFIED' if b_cert else 'NOT certified'}")
            not_certified = not (a_cert or b_cert)
        emit()
    emit("```")
    emit()
    emit(f"**Q5 NOT CERTIFIED verdict {'STANDS' if not_certified else 'FLIPS'} on the "
         f"current book.**")
    return not_certified


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-root", type=Path, default=Path(".cache"))
    ap.add_argument("--as-of", default="20260703", metavar="YYYYMMDD")
    ap.add_argument("--repass-cache", type=Path, default=None,
                    help="load derived contexts from this parquet if present, else write it")
    ap.add_argument("--limit", type=int, default=0,
                    help="detector re-pass: only the first N tickers (smoke tests)")
    ap.add_argument("--progress-every", type=int, default=50)
    ap.add_argument("--out", type=Path, default=Path("results_regrade.md"))
    args = ap.parse_args()

    emit("# Part-A re-grade: current-target reversal book vs recorded legacy values")
    emit()
    emit(f"Generated {datetime.now():%Y-%m-%d %H:%M} | corpus as-of {args.as_of} | "
         f"slip 0.05 baked in | seeded house bootstraps (fully deterministic)")
    emit()
    legacy, current = load_books(args.cache_root)
    emit("- legacy = D_dump (pre-flip reversal_retrace_frac 0.786 targets); "
         "current = R_stop_a variant=default (retrace 1.0 targets); "
         "entries/stops/fills shared")

    agreement_section(legacy, current)
    q1 = run_q1(legacy, current)
    q2 = run_q2(legacy, current)

    if args.repass_cache is not None and args.repass_cache.exists():
        ctx = pd.read_parquet(args.repass_cache)
        ctx["opened_date"] = _to_date(ctx["opened_date"])
        ctx["flip_date"] = _to_date(ctx["flip_date"])
        print(f"contexts loaded from cache: {len(ctx)} pairs", flush=True)
    else:
        pairs: dict[str, set] = defaultdict(set)
        for t, od in zip(legacy["ticker"], legacy["opened_date"]):
            pairs[t].add(od)
        if args.limit:
            pairs = dict(sorted(pairs.items())[: args.limit])
        n_pairs = sum(len(v) for v in pairs.values())
        print(f"detector re-pass over {len(pairs)} tickers, {n_pairs} pairs ...",
              flush=True)
        ctx = derive_context(pairs, args.cache_root, args.as_of, args.progress_every)
        if args.repass_cache is not None:
            args.repass_cache.parent.mkdir(parents=True, exist_ok=True)
            ctx.to_parquet(args.repass_cache)
            print(f"contexts cached -> {args.repass_cache}", flush=True)

    smoke = bool(args.limit)
    q4 = run_q4(legacy, current, ctx, args.cache_root, args.as_of, smoke)
    q5 = run_q5(legacy, current, ctx, smoke)

    emit()
    emit("## Summary")
    emit()
    emit("| diagnostic | recorded (legacy book) | current-book verdict |")
    emit("|---|---|---|")
    emit(f"| Q1 crosstab | bear x high {RECORDED['q1_bearxhigh']} | verdict sentence "
         f"{'SURVIVES' if q1 else 'DOES NOT SURVIVE'} |")
    emit(f"| Q2 early x high A | {RECORDED['q2_cellA']} | bar "
         f"{'PASSES' if q2 else 'FAILS'} |")
    emit(f"| Q4 rotation | {RECORDED['q4_delta']} | RETIRE "
         f"{'STANDS' if q4 else 'CHALLENGED'} |")
    emit(f"| Q5 tier ladder | A1 {RECORDED['q5_a1']}; A2 {RECORDED['q5_a2']} | "
         f"NOT CERTIFIED {'STANDS' if q5 else 'FLIPS'} |")

    args.out.write_text("\n".join(_MD) + "\n", encoding="utf-8")
    print(f"\nresults written -> {args.out}", flush=True)


if __name__ == "__main__":
    main()

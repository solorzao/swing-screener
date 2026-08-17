"""Q9 cont_nonha_trigger: grade the raw-candle trigger kinds against the HA flip.

D1 measured that the shipped HA-flip entry pays a median 1.56 ATR above the setup's own low
against 1.81 ATR of total risk -- ~86% of every R risked is give-back, because a SMOOTHED
series cannot fire at the low. Q8 showed reclaiming that give-back is worth +0.146R but
cost 60% of the fills (it only entered on setups that retraced). These triggers fire nearer
the low on the RAW candle instead, so the price improvement should arrive WITHOUT waiting
for a retrace.

Grades ``play_type == "continuation"`` on the X_trig_a / X_trig_b dumps (0.05 ATR slippage
baked into ``realized_r``). Read-only over the cache; writes markdown to --out.

Order of operations (per docs/plans/2026-08-16-nonha-trigger-preregistration.md):

1. SANITY ANCHOR (gate): X_trig_a "default" must reproduce the pinned book and be
   row-identical to D_dump. Fails -> STOP.
2. CONTAMINATION: trigger_kind is continuation-only -> reversal books identical.
3. MAIN TABLE per kind: n_signals, fill%, n_closed, clusters, expectancy, clustered lb,
   half-width, flags.
4. MECHANISM CHECK (mandatory, pre-registered): the give-back ratio per kind, recomputed
   from the booked trades. A kind that does not materially reduce it has NOT tested the
   hypothesis and its expectancy may not be read as evidence about entry timing.
5. Exit-reason mix -- tells a change in WHAT HAPPENS from a change in the R denominator.
6. Pre-registered verdict: POSITIVE iff some non-HA kind clears clustered lb > 0.

    PYTHONPATH=src python scripts/replay_nonha_trigger.py --cache-root .cache
"""

import argparse
import glob
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, cast

import numpy as np
import pandas as pd

from swing_screener.analytics.performance import PerformanceSummary, summarize

if TYPE_CHECKING:
    from swing_screener.db.models import PaperTrade

# (walk tag, variant name) in grid order; "default" is the shipped HA flip and the anchor.
_KINDS: tuple[tuple[str, str], ...] = (
    ("X_trig_a", "default"),
    ("X_trig_a", "raw_up"),
    ("X_trig_a", "raw_reclaim"),
    ("X_trig_b", "raw_reclaim_hl"),
)
_ANCHOR = "default"
_EXIT_REASONS = ("target", "stop", "momentum_flip", "time_stop")

_N_MIN, _CL_MIN, _HW_BAR = 20, 8, 0.10
_ANCH_EXP, _ANCH_EXP_TOL = -0.161, 0.005
_ANCH_N_LO, _ANCH_N_HI = 15_900, 16_500
_ANCH_CL_MIN = 500
_ANCH_FILL_LO, _ANCH_FILL_HI = 0.95, 0.99
# D1's measured baseline for the mechanism check (medians over 16,868 triggers).
_D1_GIVEBACK = 0.86

_KEY = ["ticker", "entry_date", "opened_date"]
_ID_STR = ["fill_status", "status", "exit_reason", "exit_date"]
_ID_NUM = ["entry_price", "stop", "target", "risk", "realized_r", "hold_bars"]

_MD: list[str] = []


def _emit(line: str = "") -> None:
    print(line, flush=True)  # noqa: T201
    _MD.append(line)


class _Row:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, _: str) -> None:
        return None


def _rows(cd: pd.DataFrame) -> "list[PaperTrade]":
    cd = cd.sort_values(_KEY, kind="mergesort")
    return cast("list[PaperTrade]", [
        _Row(ticker=r.ticker, status=r.status, fill_status=r.fill_status,
             realized_r=None if pd.isna(r.realized_r) else float(r.realized_r),
             hold_bars=None if pd.isna(r.hold_bars) else float(r.hold_bars))
        for r in cd.itertuples()
    ])


def _load(cache_root: Path, tag: str, slip: str) -> pd.DataFrame:
    pat = str(cache_root / "queue_experiments" / f"{tag}_s*_slip{slip}.parquet")
    files = sorted(glob.glob(pat))
    if not files:
        raise SystemExit(f"no shards match {pat}")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    _emit(f"- loaded `{tag}`: {len(files)} shards, {len(df)} rows, "
          f"variants {sorted(df.variant.unique())}")
    return df


def _fill_pct(cd: pd.DataFrame) -> tuple[int, int, float]:
    n_f = int((cd.fill_status == "filled").sum())
    n_m = int((cd.fill_status == "missed").sum())
    return n_f, n_m, (n_f / (n_f + n_m) if (n_f + n_m) else 0.0)


def _keyed_diff(a: pd.DataFrame, b: pd.DataFrame) -> tuple[int, int, int, dict[str, int]]:
    for name, df in (("left", a), ("right", b)):
        n_dup = int(df.duplicated(_KEY).sum())
        if n_dup:
            raise SystemExit(f"join key not unique on the {name} book ({n_dup} dups)")
    m = a.merge(b, on=_KEY, suffixes=("_a", "_b"), how="outer", indicator=True)
    both = m[m._merge == "both"]
    diffs: dict[str, int] = {}
    for c in _ID_NUM:
        diffs[c] = int((~np.isclose(both[f"{c}_a"].astype(float),
                                    both[f"{c}_b"].astype(float),
                                    atol=1e-9, equal_nan=True)).sum())
    for c in _ID_STR:
        sa, sb = both[f"{c}_a"], both[f"{c}_b"]
        diffs[c] = int(((sa.astype(str) != sb.astype(str)) & ~(sa.isna() & sb.isna())).sum())
    return (int((m._merge == "both").sum()), int((m._merge == "left_only").sum()),
            int((m._merge == "right_only").sum()), diffs)


def _anchor_gate(a: pd.DataFrame, dd: pd.DataFrame) -> None:
    _emit("\n## 1. Sanity anchor (gate)")
    c = a[(a.variant == _ANCHOR) & (a.play_type == "continuation")]
    s = summarize(_rows(c))
    n_f, n_m, fill = _fill_pct(c)
    _emit(f"\nX_trig_a `default` continuation book: n_signals={len(c)}, filled={n_f}, "
          f"missed={n_m}, fill%={fill:.1%}, n_closed={s.n_closed}, clusters={s.n_clusters}, "
          f"expectancy={s.expectancy_r:+.4f}R, clustered lb95={s.expectancy_ci_low:+.4f}R")
    checks = [
        (f"expectancy within +/-{_ANCH_EXP_TOL} of {_ANCH_EXP}R",
         abs(s.expectancy_r - _ANCH_EXP) <= _ANCH_EXP_TOL, f"{s.expectancy_r:+.4f}R"),
        (f"n_closed in [{_ANCH_N_LO}, {_ANCH_N_HI}]",
         _ANCH_N_LO <= s.n_closed <= _ANCH_N_HI, str(s.n_closed)),
        (f"clusters >= {_ANCH_CL_MIN}", s.n_clusters >= _ANCH_CL_MIN, str(s.n_clusters)),
        (f"fill% in [{_ANCH_FILL_LO:.0%}, {_ANCH_FILL_HI:.0%}]",
         _ANCH_FILL_LO <= fill <= _ANCH_FILL_HI, f"{fill:.1%}"),
    ]
    for label, ok, detail in checks:
        _emit(f"- {'PASS' if ok else 'FAIL'}: {label} ({detail})")
    d = dd[dd.play_type == "continuation"]
    n_both, n_a, n_b, diffs = _keyed_diff(c, d)
    total = n_both + n_a + n_b
    _emit(f"\nCross-walk identity vs D_dump default continuation: {n_both}/{total} keys "
          f"matched ({n_both / total:.2%}); only-in-sweep={n_a}, only-in-D_dump={n_b}")
    _emit(f"- per-column mismatches: {diffs}")
    identity_ok = n_a == 0 and n_b == 0 and all(v == 0 for v in diffs.values())
    _emit(f"- {'PASS' if identity_ok else 'FAIL'}: trade sets identical")
    if not (all(ok for _, ok, _ in checks) and identity_ok):
        _emit("\n**ANCHOR FAILED - stopping without grading any cell** (pre-registered gate).")
        raise SystemExit(1)
    _emit("\n**Anchor PASS** - the walk reproduces the pinned book; cells are gradable.")


def _reversal_multiset(df: pd.DataFrame, variant: str) -> Counter:
    cols = [c for c in df.columns if c != "variant"]
    x = df[(df.variant == variant) & (df.play_type == "reversal")][cols].astype(str)
    return Counter(map(tuple, x.itertuples(index=False, name=None)))


def _contamination(a: pd.DataFrame, b: pd.DataFrame) -> None:
    _emit("\n## 2. Contamination check (reversal cohorts)")
    base = _reversal_multiset(a, _ANCHOR)
    n_base = sum(base.values())
    _emit(f"\nBaseline = X_trig_a `default` reversal book ({n_base} rows).")
    all_ok = True
    for walk, df in (("X_trig_a", a), ("X_trig_b", b)):
        for v in sorted(df.variant.unique()):
            if walk == "X_trig_a" and v == _ANCHOR:
                continue
            m = _reversal_multiset(df, v)
            ok = m == base
            all_ok &= ok
            _emit(f"- {'PASS' if ok else 'FAIL'}: {walk}/{v} -- {sum(m.values())} rows, "
                  f"{sum((m & base).values())}/{n_base} identical")
    _emit(f"\nVerdict: {'PASS' if all_ok else 'FAIL'} -- the trigger knob "
          f"{'provably does not touch' if all_ok else 'LEAKED INTO'} the reversal book.")


def _cell_flags(s: PerformanceSummary, hw: float) -> str:
    flags = []
    if s.n_closed < _N_MIN or s.n_clusters < _CL_MIN:
        flags.append("thin")
    if hw > _HW_BAR:
        flags.append(f"hw>{_HW_BAR}R: non-decisional")
    return ", ".join(flags) if flags else "decisional"


def _main_table(
    books: dict[str, pd.DataFrame],
) -> dict[str, tuple[PerformanceSummary, float, float]]:
    _emit("\n## 3. Main table -- continuation cohort per trigger kind (slip 0.05)")
    _emit("\n`n_signals` sits next to every expectancy: a looser trigger fires more often, "
          "and more signals is not the same as more opportunity.")
    _emit("\n| trigger | n_signals | filled | missed | fill% | n_closed | clusters "
          "| exp R | lb95 (clust) | half-width | flags |")
    _emit("|---|---|---|---|---|---|---|---|---|---|---|")
    out: dict[str, tuple[PerformanceSummary, float, float]] = {}
    for kind, cd in books.items():
        n_f, n_m, fill = _fill_pct(cd)
        s = summarize(_rows(cd))
        hw = s.expectancy_r - s.expectancy_ci_low
        tag = " (HA, shipped)" if kind == _ANCHOR else ""
        _emit(f"| {kind}{tag} | {len(cd)} | {n_f} | {n_m} | {fill:.1%} | {s.n_closed} "
              f"| {s.n_clusters} | {s.expectancy_r:+.3f} | {s.expectancy_ci_low:+.3f} "
              f"| {hw:.3f} | {_cell_flags(s, hw)} |")
        out[kind] = (s, fill, hw)
    return out


def _mechanism(books: dict[str, pd.DataFrame]) -> dict[str, float]:
    """Step 4 (MANDATORY, pre-registered): did the trigger actually fire nearer the low?

    Recomputed from the booked trades: ``risk`` is ``entry_ceiling - stop`` and the stop is
    ``swing_low - stop_buffer_atr * atr``, so the give-back (entry above the setup's low)
    is ``risk - stop_buffer_atr * atr``. ATR is not dumped, so the ratio is reported in the
    dump's own terms: mean risk per kind, and mean risk RELATIVE to the anchor."""
    _emit("\n## 4. Mechanism check (mandatory)")
    _emit("\nD1 baseline: the shipped entry pays a median **1.56 ATR** above the setup's "
          "own low against **1.81 ATR** of risk -- a give-back ratio of ~**86%**. A kind "
          "that does not materially shrink `risk` has NOT fired nearer the low, and its "
          "expectancy may not be read as evidence about entry timing.")
    base = books[_ANCHOR]
    base_risk = float(base[base.fill_status == "filled"].risk.astype(float).mean())
    _emit("\n| trigger | mean risk | vs anchor | reading |")
    _emit("|---|---|---|---|")
    out: dict[str, float] = {}
    for kind, cd in books.items():
        f = cd[cd.fill_status == "filled"]
        r = float(f.risk.astype(float).mean()) if len(f) else float("nan")
        rel = r / base_risk if base_risk else float("nan")
        out[kind] = rel
        if kind == _ANCHOR:
            reading = "anchor"
        elif rel <= 0.90:
            reading = "fires nearer the low (hypothesis tested)"
        else:
            reading = "**NOT nearer the low -- hypothesis untested by this cell**"
        _emit(f"| {kind} | {r:.3f} | {rel:.3f}x | {reading} |")
    return out


def _exit_mix(books: dict[str, pd.DataFrame]) -> None:
    _emit("\n## 5. Exit-reason mix (closed-filled continuation trades)")
    _emit("\nTells a change in WHAT HAPPENS from a change in the R denominator: a smaller "
          "risk mechanically lifts R-to-target, but only a real improvement moves the "
          "target/stop MIX.")
    hdr = " | ".join(f"{r}%" for r in _EXIT_REASONS)
    _emit(f"\n| trigger | n_closed | {hdr} | avg hold |")
    _emit("|---|---|" + "---|" * (len(_EXIT_REASONS) + 1))
    for kind, cd in books.items():
        cf = cd[(cd.status == "closed") & (cd.fill_status == "filled")
                & cd.realized_r.notna()]
        mix = cf.exit_reason.value_counts(normalize=True)
        cells = " | ".join(f"{mix.get(r, 0.0):.1%}" for r in _EXIT_REASONS)
        hold = cf.hold_bars.astype(float).mean() if len(cf) else float("nan")
        _emit(f"| {kind} | {len(cf)} | {cells} | {hold:.1f} |")


def _verdict(graded: dict[str, tuple[PerformanceSummary, float, float]],
             mech: dict[str, float]) -> str:
    _emit("\n## 6. Pre-registered verdict (one-shot; no grid extension)")
    _emit("\nDecisional iff n_closed >= 20, clusters >= 8, half-width <= 0.10R. POSITIVE "
          "iff some NON-HA kind has clustered 95% lb > 0 net of 0.05 ATR.")
    _emit()
    winners = []
    for kind, (s, fill, hw) in graded.items():
        decisional = s.n_closed >= _N_MIN and s.n_clusters >= _CL_MIN and hw <= _HW_BAR
        clears = decisional and s.expectancy_ci_low > 0
        if clears and kind != _ANCHOR:
            winners.append(kind)
        note = "" if kind == _ANCHOR or mech.get(kind, 1.0) <= 0.90 else \
            " [mechanism NOT engaged]"
        _emit(f"- {kind}: lb={s.expectancy_ci_low:+.3f}R (exp {s.expectancy_r:+.3f}R, "
              f"fill {fill:.1%}, n={s.n_closed}, cl={s.n_clusters}, hw={hw:.3f}R) -> "
              f"{'CLEARS lb>0' if clears else 'does not clear'}"
              f"{'' if decisional else ' [non-decisional]'}{note}")
    if winners:
        _emit(f"\n**VERDICT: POSITIVE** -- {winners} clear lb > 0 at 0.05 ATR. Evidence "
              "caps at REPLAY-SCREENED: promotion needs a 0.10-slippage robustness re-walk "
              "and a forward variant slot, and un-parking continuation remains a separate "
              "human-gated decision on forward evidence.")
        return "POSITIVE"
    _emit("\n**VERDICT: NULL** -- no non-HA trigger clears clustered 95% lb > 0 net of 0.05 "
          "ATR. Firing nearer the low was the single mechanism D1 identified and Q8 "
          "corroborated; with it exhausted, entry TIMING joins entry PRICE as a falsified "
          "explanation. The remaining D1 finding (>10% of 'shallow pauses' retrace their "
          "entire leg) is the last structural candidate.")
    return "NULL"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-root", type=Path, default=Path(".cache"))
    ap.add_argument("--slip", default="0.05")
    ap.add_argument("--out", type=Path, default=Path("results_q9.md"))
    args = ap.parse_args()

    _emit("# Q9 cont_nonha_trigger -- raw-candle triggers vs the HA flip")
    _emit(f"\nPinned corpus as-of 20260703; slippage {args.slip} ATR baked into realized_r; "
          "house ticker-clustered bootstrap (seeded, canonical order); continuation cohort "
          "only. Pre-registration: docs/plans/2026-08-16-nonha-trigger-preregistration.md")
    _emit()
    a = _load(args.cache_root, "X_trig_a", args.slip)
    b = _load(args.cache_root, "X_trig_b", args.slip)
    dd = _load(args.cache_root, "D_dump", args.slip)

    _anchor_gate(a, dd)
    _contamination(a, b)

    books: dict[str, pd.DataFrame] = {}
    for walk, variant in _KINDS:
        df = a if walk == "X_trig_a" else b
        books[variant] = df[(df.variant == variant) & (df.play_type == "continuation")]

    graded = _main_table(books)
    mech = _mechanism(books)
    _exit_mix(books)
    _verdict(graded, mech)

    _emit("\n## 7. Caveats")
    _emit("\n- Replay-screened evidence tier only; nothing here is forward-confirmed.")
    _emit("- Only slip-0.05 dumps exist; the 0.10 robustness re-walk is conditional on "
          "POSITIVE.")
    _emit("- The pullback walk-back and shaved-head requirement remain HA-based; only the "
          "TRIGGER bar's test changed. A fully raw setup definition is a different, larger "
          "experiment.")
    _emit("- An undercut-and-reclaim ('spring') trigger was deliberately excluded: the "
          "setup's swing_low excludes the trigger bar, so such a trigger would place the "
          "stop above its own bar's low. Testing it needs a stop-geometry change too.")
    _emit("- The seeded clustered bootstrap is ticker-insertion-order sensitive at the 3rd "
          "decimal; rows are canonically sorted before every bound.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(_MD) + "\n", encoding="utf-8")
    print(f"\n[results written to {args.out}]", flush=True)  # noqa: T201


if __name__ == "__main__":
    main()

"""Q6 cont_ceiling_sweep_then_park: grade the ceiling_atr_mult sweep dumps.

Grades the pinned queue-experiment ceiling sweep (C_ceil_lo: ceil_015 / ceil_025 /
default-0.35; C_ceil_hi: ceil_045 / ceil_055 / ceil_065; 0.05 ATR slippage baked into
``realized_r``) on ``play_type == "continuation"``. Read-only over the cache; writes the
results markdown to ``--out``.

Order of operations (per the 2026-07-25 pre-registration, Q6):

1. SANITY ANCHOR (gate): the C_ceil_lo "default" continuation book must reproduce the
   pinned book (~-0.161R, n_closed ~16.2k, ~511 clusters, ~97% fill) and must be
   row-identical to the pre-existing D_dump default continuation book. Fails -> STOP.
2. CONTAMINATION: the reversal cohorts must be identical across all 6 ceiling values
   (the knob is continuation-only); cross-walk vs D_dump reported with per-column diffs.
3. MAIN TABLE per ceiling: n_signals, fill% (filled/(filled+missed)) NEXT TO every
   expectancy (the expectancy-wins-while-never-filling artifact watch), n_closed,
   clusters, expectancy, ticker-clustered 95% lb, half-width (exp - clustered lb, the
   hardened definition; > 0.10R -> reported-but-non-decisional), exit-reason mix, and a
   paired vs-default read (what fills vs how much).
4. Pre-registered verdict: decisional iff n_closed >= 20 and clusters >= 8; NULL = no
   value's continuation lb > 0 at 0.05 slip -> the parking rule activates (surfacing-only
   ``surface_continuation`` flag ships as a separate human-gated PR). One-shot grid.

    PYTHONPATH=src python scripts/replay_ceil_sweep.py --cache-root .cache
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

# (walk tag, variant name, ceiling_atr_mult) in grid order. 0.35 is the live default.
_CEILINGS: tuple[tuple[str, str, float], ...] = (
    ("C_ceil_lo", "ceil_015", 0.15),
    ("C_ceil_lo", "ceil_025", 0.25),
    ("C_ceil_lo", "default", 0.35),
    ("C_ceil_hi", "ceil_045", 0.45),
    ("C_ceil_hi", "ceil_055", 0.55),
    ("C_ceil_hi", "ceil_065", 0.65),
)
_EXIT_REASONS = ("target", "stop", "momentum_flip", "time_stop")

# Pre-registered decisional bar + the hardened half-width cap (exp - clustered lb).
_N_MIN, _CL_MIN, _HW_BAR = 20, 8, 0.10

# Anchor tolerances: the pinned continuation book (-0.161R, n~16.2k, 511 clusters, ~97%).
_ANCH_EXP, _ANCH_EXP_TOL = -0.161, 0.005
_ANCH_N_LO, _ANCH_N_HI = 15_900, 16_500
_ANCH_CL_MIN = 500
_ANCH_FILL_LO, _ANCH_FILL_HI = 0.95, 0.99

# (ticker, entry_date, opened_date) is verified unique per book before use as the join key.
_KEY = ["ticker", "entry_date", "opened_date"]
_ID_STR = ["fill_status", "status", "exit_reason", "exit_date"]
_ID_NUM = ["entry_price", "stop", "target", "risk", "realized_r", "hold_bars"]

_MD: list[str] = []


def _emit(line: str = "") -> None:
    print(line, flush=True)  # noqa: T201
    _MD.append(line)


class _Row:
    """Minimal trade-like object so ``summarize`` can grade a parquet dump row."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, _: str) -> None:
        return None


def _rows(cd: pd.DataFrame) -> "list[PaperTrade]":
    # Sort before building rows: the seeded clustered bootstrap is ticker-insertion-order
    # sensitive at the 3rd decimal, so a canonical order keeps every bound reproducible.
    cd = cd.sort_values(_KEY, kind="mergesort")
    rows = [
        _Row(
            ticker=r.ticker,
            status=r.status,
            fill_status=r.fill_status,
            realized_r=None if pd.isna(r.realized_r) else float(r.realized_r),
            hold_bars=None if pd.isna(r.hold_bars) else float(r.hold_bars),
        )
        for r in cd.itertuples()
    ]
    return cast("list[PaperTrade]", rows)


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
    """Outer-join two books on the (verified-unique) key; per-column mismatch counts."""
    for name, df in (("left", a), ("right", b)):
        n_dup = int(df.duplicated(_KEY).sum())
        if n_dup:
            raise SystemExit(f"join key {_KEY} not unique on the {name} book ({n_dup} dups)")
    m = a.merge(b, on=_KEY, suffixes=("_a", "_b"), how="outer", indicator=True)
    n_both = int((m._merge == "both").sum())
    n_a = int((m._merge == "left_only").sum())
    n_b = int((m._merge == "right_only").sum())
    both = m[m._merge == "both"]
    diffs: dict[str, int] = {}
    for c in _ID_NUM:
        neq = ~np.isclose(both[f"{c}_a"].astype(float), both[f"{c}_b"].astype(float),
                          atol=1e-9, equal_nan=True)
        diffs[c] = int(neq.sum())
    for c in _ID_STR:
        sa, sb = both[f"{c}_a"], both[f"{c}_b"]
        # Null-safe: None-vs-NaN is a parquet representation artifact, not a diff.
        diffs[c] = int(((sa.astype(str) != sb.astype(str)) & ~(sa.isna() & sb.isna())).sum())
    return n_both, n_a, n_b, diffs


def _anchor_gate(lo: pd.DataFrame, dd: pd.DataFrame) -> None:
    """Step 1: reproduce the pinned continuation book, or stop without grading a cell."""
    _emit("\n## 1. Sanity anchor (gate)")
    c = lo[(lo.variant == "default") & (lo.play_type == "continuation")]
    s = summarize(_rows(c))
    n_f, n_m, fill = _fill_pct(c)
    _emit(f"\nC_ceil_lo `default` continuation book: n_signals={len(c)}, "
          f"filled={n_f}, missed={n_m}, fill%={fill:.1%}, n_closed={s.n_closed}, "
          f"clusters={s.n_clusters}, expectancy={s.expectancy_r:+.4f}R, "
          f"clustered lb95={s.expectancy_ci_low:+.4f}R")
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
    _emit(f"\nCross-walk identity vs D_dump default continuation "
          f"(key = ticker+entry_date+opened_date, verified unique): "
          f"{n_both}/{total} keys matched ({n_both / total:.2%}); "
          f"only-in-sweep={n_a}, only-in-D_dump={n_b}")
    _emit(f"- per-column mismatches on matched keys: {diffs}")
    identity_ok = n_a == 0 and n_b == 0 and all(v == 0 for v in diffs.values())
    _emit(f"- {'PASS' if identity_ok else 'FAIL'}: trade sets identical "
          f"(every column, realized_r to 1e-9)")

    if not (all(ok for _, ok, _ in checks) and identity_ok):
        _emit("\n**ANCHOR FAILED — stopping without grading any sweep cell** "
              "(pre-registered gate).")
        raise SystemExit(1)
    _emit("\n**Anchor PASS** — the sweep walk reproduces the pinned book; cells are gradable.")


def _reversal_multiset(df: pd.DataFrame, variant: str) -> Counter:
    cols = [c for c in df.columns if c != "variant"]
    x = df[(df.variant == variant) & (df.play_type == "reversal")][cols].astype(str)
    return Counter(map(tuple, x.itertuples(index=False, name=None)))


def _contamination(lo: pd.DataFrame, hi: pd.DataFrame, dd: pd.DataFrame) -> None:
    """Step 2: ceiling_atr_mult is continuation-only -> reversal books must be identical."""
    _emit("\n## 2. Contamination check (reversal cohorts)")
    base = _reversal_multiset(lo, "default")
    n_base = sum(base.values())
    _emit(f"\nBaseline = C_ceil_lo `default` reversal book ({n_base} rows). Row-multiset "
          f"identity over every dump column except `variant`:")
    all_ok = True
    for walk, df in (("C_ceil_lo", lo), ("C_ceil_hi", hi)):
        for v in sorted(df.variant.unique()):
            if walk == "C_ceil_lo" and v == "default":
                continue
            m = _reversal_multiset(df, v)
            common = sum((m & base).values())
            ok = m == base
            all_ok &= ok
            _emit(f"- {'PASS' if ok else 'FAIL'}: {walk}/{v} -- {sum(m.values())} rows, "
                  f"{common}/{n_base} identical to baseline")
    if all_ok:
        _emit("\nWithin-sweep verdict: PASS -- all 6 ceiling values share a byte-identical "
              "reversal book; the ceiling knob provably does not touch reversal.")
    else:
        _emit("\nWithin-sweep verdict: FAIL -- the ceiling knob leaked into the reversal "
              "book.")

    a = lo[(lo.variant == "default") & (lo.play_type == "reversal")]
    b = dd[dd.play_type == "reversal"]
    n_both, n_a, n_b, diffs = _keyed_diff(a, b)
    total = n_both + n_a + n_b
    dd_ok = n_a == 0 and n_b == 0 and all(v == 0 for v in diffs.values())
    _emit(f"\nCross-walk vs D_dump default reversal book: {n_both}/{total} keys matched; "
          f"per-column mismatches: {diffs}")
    _emit(f"- {'PASS' if dd_ok else 'FAIL'}: identical to D_dump")
    if not dd_ok and all_ok:
        _emit("- Reading: NOT ceiling contamination (all 6 values are identical to each "
              "other, including both fresh walk invocations) -- the pre-existing D_dump "
              "reversal book is a different TARGET VINTAGE: same signals/entries/stops/"
              "fills, but `target` differs, moving exits. Flag for any sibling analysis "
              "that mixes D_dump reversal cells with fresh-walk reversal cells.")


def _summ(cd: pd.DataFrame) -> PerformanceSummary:
    return summarize(_rows(cd))


def _cell_flags(s: PerformanceSummary, hw: float) -> str:
    flags = []
    if s.n_closed < _N_MIN or s.n_clusters < _CL_MIN:
        flags.append("thin")
    if hw > _HW_BAR:
        flags.append(f"hw>{_HW_BAR}R: non-decisional")
    return ", ".join(flags) if flags else "decisional"


def _main_table(
    books: dict[float, pd.DataFrame],
) -> dict[float, tuple[PerformanceSummary, float, float]]:
    """Step 3: the per-ceiling continuation grade. Returns {ceiling: (summary, fill, hw)}."""
    _emit("\n## 3. Main table -- continuation cohort per ceiling_atr_mult (slip 0.05)")
    _emit("\nfill% sits NEXT TO every expectancy (artifact watch: a tight-ceiling cell "
          "that 'wins' on a sliver of fills must be visible as such).")
    _emit("\n| ceiling | n_signals | filled | missed | inval | fill% | n_closed | clusters "
          "| exp R | lb95 (clust) | half-width | flags |")
    _emit("|---|---|---|---|---|---|---|---|---|---|---|---|")
    out: dict[float, tuple[PerformanceSummary, float, float]] = {}
    for ceil, cd in books.items():
        n_f, n_m, fill = _fill_pct(cd)
        n_inv = int((cd.fill_status == "invalidated").sum())
        s = _summ(cd)
        hw = s.expectancy_r - s.expectancy_ci_low
        tag = " (default)" if ceil == 0.35 else ""
        _emit(f"| {ceil:.2f}{tag} | {len(cd)} | {n_f} | {n_m} | {n_inv} | {fill:.1%} "
              f"| {s.n_closed} | {s.n_clusters} | {s.expectancy_r:+.3f} "
              f"| {s.expectancy_ci_low:+.3f} | {hw:.3f} | {_cell_flags(s, hw)} |")
        out[ceil] = (s, fill, hw)
    return out


def _exit_mix(books: dict[float, pd.DataFrame]) -> None:
    _emit("\n## 4. Exit-reason mix per ceiling (closed-filled continuation trades)")
    _emit("\nThe mechanism read: does a tighter ceiling change WHAT fills, or just how "
          "the same trades resolve?")
    hdr = " | ".join(f"{r}%" for r in _EXIT_REASONS)
    _emit(f"\n| ceiling | n_closed | {hdr} | avg hold (bars) |")
    _emit("|---|---|" + "---|" * (len(_EXIT_REASONS) + 1))
    for ceil, cd in books.items():
        cf = cd[(cd.status == "closed") & (cd.fill_status == "filled")
                & cd.realized_r.notna()]
        mix = cf.exit_reason.value_counts(normalize=True)
        cells = " | ".join(f"{mix.get(r, 0.0):.1%}" for r in _EXIT_REASONS)
        hold = cf.hold_bars.astype(float).mean()
        _emit(f"| {ceil:.2f} | {len(cf)} | {cells} | {hold:.1f} |")


def _vs_default(books: dict[float, pd.DataFrame]) -> None:
    _emit("\n## 5. Paired read vs default (what fills vs how much)")
    _emit("\nFILLED-set overlap on (ticker, entry_date): filled rows always carry "
          "entry_date == opened_date (the fill day) and are key-unique, so this is the "
          "clean fill identity. A missed booking re-anchors entry_date, so a "
          "filled<->missed flip is structurally unobservable by key -- the "
          "'only default'/'only this' columns ARE the fills the ceiling adds/removes. "
          "Descriptive only; no paired bound is claimed.")
    key = ["ticker", "entry_date"]
    base = books[0.35]
    base_f = base[base.fill_status == "filled"]
    _emit("\n| ceiling | both filled | filled only this | filled only default "
          "| fill-day shifts | entry-price diffs | both-closed pairs "
          "| paired mean dR (this - default) |")
    _emit("|---|---|---|---|---|---|---|---|")
    for ceil, cd in books.items():
        if ceil == 0.35:
            continue
        f = cd[cd.fill_status == "filled"]
        for name, df in (("variant", f), ("default", base_f)):
            n_dup = int(df.duplicated(key).sum())
            if n_dup:
                raise SystemExit(f"filled rows not key-unique on the {name} book "
                                 f"(ceiling {ceil}: {n_dup} dups)")
        m = f.merge(base_f, on=key, suffixes=("_v", "_d"), how="outer", indicator=True)
        n_both = int((m._merge == "both").sum())
        n_v = int((m._merge == "left_only").sum())
        n_d = int((m._merge == "right_only").sum())
        both = m[m._merge == "both"]
        shifts = int((both.opened_date_v != both.opened_date_d).sum())
        px = int((~np.isclose(both.entry_price_v.astype(float),
                              both.entry_price_d.astype(float), atol=1e-9)).sum())
        closed = both[(both.status_v == "closed") & (both.status_d == "closed")
                      & both.realized_r_v.notna() & both.realized_r_d.notna()]
        drr = (closed.realized_r_v - closed.realized_r_d).mean() if len(closed) else 0.0
        _emit(f"| {ceil:.2f} | {n_both} | {n_v} | {n_d} | {shifts} | {px} "
              f"| {len(closed)} | {drr:+.4f}R |")


def _verdict(graded: dict[float, tuple[PerformanceSummary, float, float]]) -> str:
    _emit("\n## 6. Pre-registered verdict (one-shot; no grid extension)")
    _emit("\nDecisional iff n_closed >= 20 and clusters >= 8 (half-width > 0.10R -> "
          "reported-but-non-decisional). NULL = no swept value's continuation cohort has "
          "clustered 95% lb > 0 net of 0.05 ATR.")
    _emit()
    winners = []
    for ceil, (s, fill, hw) in graded.items():
        decisional = s.n_closed >= _N_MIN and s.n_clusters >= _CL_MIN and hw <= _HW_BAR
        clears = decisional and s.expectancy_ci_low > 0
        if clears:
            winners.append(ceil)
        _emit(f"- ceiling {ceil:.2f}: lb={s.expectancy_ci_low:+.3f}R "
              f"(exp {s.expectancy_r:+.3f}R, fill {fill:.1%}, n={s.n_closed}, "
              f"cl={s.n_clusters}, hw={hw:.3f}R) -> "
              f"{'CLEARS lb>0' if clears else 'does not clear'}"
              f"{'' if decisional else ' [non-decisional]'}")
    if winners:
        v = "POSITIVE"
        _emit(f"\n**VERDICT: POSITIVE** -- ceiling(s) {winners} clear lb > 0 at 0.05 ATR. "
              "Evidence caps at replay-screened; a 0.10-slippage robustness re-walk is "
              "required before anything more, and promotion needs a retired variant slot "
              "(7-book ceiling).")
    else:
        v = "NULL"
        _emit("\n**VERDICT: NULL** -- no swept ceiling's continuation cohort clears "
              "clustered 95% lb > 0 net of 0.05 ATR. The pre-authorized parking rule "
              "activates: a separate human-gated PR adds surfacing-only "
              "`surface_continuation: bool` (mirroring `reversal_surface_confirmed_only`; "
              "continuation stays detected/scored/shadow-booked; the registered "
              "cont_volband/extguard_tight books keep accruing as formal arbiters), "
              "flipped False. That PR is NOT part of this analysis. One-shot: the value "
              "grid is closed.")
    return v


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-root", type=Path, default=Path(".cache"))
    ap.add_argument("--slip", default="0.05", help="dump slippage suffix to grade")
    ap.add_argument("--out", type=Path, default=Path("results_q6.md"),
                    help="results markdown path")
    args = ap.parse_args()

    _emit("# Q6 cont_ceiling_sweep_then_park -- ceiling_atr_mult sweep grade")
    _emit(f"\nPinned 511-name corpus as-of 20260703; slippage {args.slip} ATR baked into "
          "realized_r; house ticker-clustered bootstrap (seeded, canonical ticker order); "
          "continuation cohort only.")
    _emit()
    lo = _load(args.cache_root, "C_ceil_lo", args.slip)
    hi = _load(args.cache_root, "C_ceil_hi", args.slip)
    dd = _load(args.cache_root, "D_dump", args.slip)

    _anchor_gate(lo, dd)
    _contamination(lo, hi, dd)

    books: dict[float, pd.DataFrame] = {}
    for walk, variant, ceil in _CEILINGS:
        df = lo if walk == "C_ceil_lo" else hi
        books[ceil] = df[(df.variant == variant) & (df.play_type == "continuation")]

    graded = _main_table(books)
    _exit_mix(books)
    _vs_default(books)
    _verdict(graded)

    _emit("\n## 7. Caveats")
    _emit("\n- Replay-screened evidence tier only (pinned corpus, deterministic walk); "
          "nothing here is forward-confirmed.")
    _emit("- Only slip-0.05 dumps exist for the C walks; the pre-registered 0.10 "
          "robustness re-walk is conditional on POSITIVE and is moot under NULL.")
    _emit("- The pre-existing D_dump REVERSAL book is a different target vintage than "
          "the fresh walks (`target` differs on all 70,277 keyed rows; realized_r on "
          "10,739): sibling analyses must not mix D_dump reversal cells with fresh-walk "
          "reversal cells. Continuation is unaffected (100% identity).")
    _emit("- The seeded clustered bootstrap is ticker-insertion-order sensitive at the "
          "3rd decimal; rows are canonically sorted before every bound, and 3rd-decimal "
          "precision should not be over-read.")

    args.out.write_text("\n".join(_MD) + "\n", encoding="utf-8")
    print(f"\n[results written to {args.out}]", flush=True)  # noqa: T201


if __name__ == "__main__":
    main()

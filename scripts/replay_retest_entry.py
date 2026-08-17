"""Q8 cont_retest_entry: grade the NEGATIVE ceiling_atr_mult sweep (retest-limit entry).

Q6 swept ceiling_atr_mult over 0.15-0.65 -- every cell a limit ABOVE the trigger close,
i.e. a variation on chasing the flip bar. It graded NULL and parked continuation. This
walk inverts the mechanic: a NEGATIVE mult puts the limit BELOW the trigger close, so the
bar must retrace into it to fill. That changes the PRICE (lower entry, unchanged stop ->
smaller risk -> the same move is a larger R) and the SELECTION (only setups that pull back
are taken) at the same time.

Grades ``play_type == "continuation"`` on the C_retest_a / C_retest_b dumps (0.05 ATR
slippage baked into ``realized_r``). Read-only over the cache; writes markdown to --out.

Order of operations (per docs/plans/2026-08-16-retest-entry-preregistration.md):

1. SANITY ANCHOR (gate): the C_retest_a "default" continuation book must reproduce the
   pinned book (~-0.161R, n_closed ~16.2k, >=500 clusters, 95-99% fill) AND be
   row-identical to the pre-existing D_dump default continuation book. Fails -> STOP.
2. CONTAMINATION: ceiling_atr_mult is continuation-only, so the reversal cohorts must be
   row-identical across all six cells.
3. MAIN TABLE per mult: n_signals, fill%, n_closed, clusters, expectancy, ticker-clustered
   95% lb, half-width, flags.
4. SELECTION ARTIFACT (this experiment's specific failure mode): as the limit drops, fill%
   falls and the survivors are the setups that pulled back -- not a random sample. Reported
   explicitly so a "win" on a sliver of fills cannot be read as an entry-price result.
5. Pre-registered verdict: decisional iff n_closed >= 20, clusters >= 8, half-width <=
   0.10R. POSITIVE iff some RETEST cell (mult <= 0) clears clustered lb > 0. One-shot grid.

    PYTHONPATH=src python scripts/replay_retest_entry.py --cache-root .cache
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

# (walk tag, variant name, ceiling_atr_mult) in grid order. +0.35 is the live default and
# the anchor; every mult <= 0 is a RETEST cell (limit at or below the trigger close).
_MULTS: tuple[tuple[str, str, float], ...] = (
    ("C_retest_a", "default", 0.35),
    ("C_retest_a", "retest_000", 0.00),
    ("C_retest_a", "retest_015", -0.15),
    ("C_retest_b", "retest_030", -0.30),
    ("C_retest_b", "retest_050", -0.50),
    ("C_retest_b", "retest_075", -0.75),
)
_DEFAULT_MULT = 0.35
_EXIT_REASONS = ("target", "stop", "momentum_flip", "time_stop")

# Pre-registered decisional bar + the hardened half-width cap (exp - clustered lb).
_N_MIN, _CL_MIN, _HW_BAR = 20, 8, 0.10

# Anchor tolerances: the pinned continuation book (-0.161R, n~16.2k, 511 clusters, ~97%).
_ANCH_EXP, _ANCH_EXP_TOL = -0.161, 0.005
_ANCH_N_LO, _ANCH_N_HI = 15_900, 16_500
_ANCH_CL_MIN = 500
_ANCH_FILL_LO, _ANCH_FILL_HI = 0.95, 0.99

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
    # Canonical sort first: the seeded clustered bootstrap is ticker-insertion-order
    # sensitive at the 3rd decimal, so this keeps every bound reproducible.
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
        diffs[c] = int(((sa.astype(str) != sb.astype(str)) & ~(sa.isna() & sb.isna())).sum())
    return n_both, n_a, n_b, diffs


def _anchor_gate(a: pd.DataFrame, dd: pd.DataFrame) -> None:
    """Step 1: reproduce the pinned continuation book, or stop without grading a cell."""
    _emit("\n## 1. Sanity anchor (gate)")
    c = a[(a.variant == "default") & (a.play_type == "continuation")]
    s = summarize(_rows(c))
    n_f, n_m, fill = _fill_pct(c)
    _emit(f"\nC_retest_a `default` continuation book: n_signals={len(c)}, "
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
    _emit(f"\nCross-walk identity vs D_dump default continuation: "
          f"{n_both}/{total} keys matched ({n_both / total:.2%}); "
          f"only-in-sweep={n_a}, only-in-D_dump={n_b}")
    _emit(f"- per-column mismatches on matched keys: {diffs}")
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
    """Step 2: ceiling_atr_mult is continuation-only -> reversal books must be identical."""
    _emit("\n## 2. Contamination check (reversal cohorts)")
    base = _reversal_multiset(a, "default")
    n_base = sum(base.values())
    _emit(f"\nBaseline = C_retest_a `default` reversal book ({n_base} rows).")
    all_ok = True
    for walk, df in (("C_retest_a", a), ("C_retest_b", b)):
        for v in sorted(df.variant.unique()):
            if walk == "C_retest_a" and v == "default":
                continue
            m = _reversal_multiset(df, v)
            common = sum((m & base).values())
            ok = m == base
            all_ok &= ok
            _emit(f"- {'PASS' if ok else 'FAIL'}: {walk}/{v} -- {sum(m.values())} rows, "
                  f"{common}/{n_base} identical to baseline")
    _emit(f"\nVerdict: {'PASS' if all_ok else 'FAIL'} -- the retest knob "
          f"{'provably does not touch' if all_ok else 'LEAKED INTO'} the reversal book.")


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
    _emit("\n## 3. Main table -- continuation cohort per ceiling_atr_mult (slip 0.05)")
    _emit("\nNegative mult = the limit sits BELOW the trigger close (a retest bid). fill% "
          "sits next to every expectancy: a cell that 'wins' on a sliver of fills must be "
          "visible as such.")
    _emit("\n| mult | kind | n_signals | filled | missed | inval | fill% | n_closed "
          "| clusters | exp R | lb95 (clust) | half-width | flags |")
    _emit("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    out: dict[float, tuple[PerformanceSummary, float, float]] = {}
    for mult, cd in books.items():
        n_f, n_m, fill = _fill_pct(cd)
        n_inv = int((cd.fill_status == "invalidated").sum())
        s = summarize(_rows(cd))
        hw = s.expectancy_r - s.expectancy_ci_low
        kind = "chase (default)" if mult == _DEFAULT_MULT else "retest"
        _emit(f"| {mult:+.2f} | {kind} | {len(cd)} | {n_f} | {n_m} | {n_inv} | {fill:.1%} "
              f"| {s.n_closed} | {s.n_clusters} | {s.expectancy_r:+.3f} "
              f"| {s.expectancy_ci_low:+.3f} | {hw:.3f} | {_cell_flags(s, hw)} |")
        out[mult] = (s, fill, hw)
    return out


def _selection_artifact(books: dict[float, pd.DataFrame]) -> None:
    """Step 4: THE failure mode of this experiment, made explicit."""
    _emit("\n## 4. Selection artifact watch")
    _emit("\nAs the limit drops, two things shrink the book for reasons that are NOT "
          "'better entry price':")
    _emit("\n- **Degenerate zones** -- when the ceiling falls to/below the zone floor "
          "there is no tradable zone at all and the setup is never booked. Visible as "
          "n_signals falling vs the default.")
    _emit("- **Retrace selection** -- among booked setups, only those that pulled back "
          "fill. The survivors are a biased subset, not a random sample.")
    _emit("\n| mult | n_signals | vs default | signals lost (degenerate) | fill% "
          "| filled n | filled vs default |")
    _emit("|---|---|---|---|---|---|---|")
    base = books[_DEFAULT_MULT]
    n_base = len(base)
    n_base_f = int((base.fill_status == "filled").sum())
    for mult, cd in books.items():
        n_f, _n_m, fill = _fill_pct(cd)
        lost = n_base - len(cd)
        _emit(f"| {mult:+.2f} | {len(cd)} | {len(cd) - n_base:+d} | {lost} | {fill:.1%} "
              f"| {n_f} | {n_f - n_base_f:+d} |")
    _emit("\nReading rule (pre-registered): a cell whose fill% collapses is a SELECTION "
          "result, not an entry-price result, however good its expectancy looks.")


def _exit_mix(books: dict[float, pd.DataFrame]) -> None:
    _emit("\n## 5. Exit-reason mix per mult (closed-filled continuation trades)")
    hdr = " | ".join(f"{r}%" for r in _EXIT_REASONS)
    _emit(f"\n| mult | n_closed | {hdr} | avg hold (bars) | avg risk |")
    _emit("|---|---|" + "---|" * (len(_EXIT_REASONS) + 2))
    for mult, cd in books.items():
        cf = cd[(cd.status == "closed") & (cd.fill_status == "filled")
                & cd.realized_r.notna()]
        mix = cf.exit_reason.value_counts(normalize=True)
        cells = " | ".join(f"{mix.get(r, 0.0):.1%}" for r in _EXIT_REASONS)
        hold = cf.hold_bars.astype(float).mean() if len(cf) else float("nan")
        risk = cf.risk.astype(float).mean() if len(cf) else float("nan")
        _emit(f"| {mult:+.2f} | {len(cf)} | {cells} | {hold:.1f} | {risk:.3f} |")
    _emit("\n`avg risk` is the mechanism check: a lower ceiling shrinks "
          "`risk = ceiling - stop`, so the same absolute move books as a larger R.")


def _verdict(graded: dict[float, tuple[PerformanceSummary, float, float]]) -> str:
    _emit("\n## 6. Pre-registered verdict (one-shot; no grid extension)")
    _emit("\nDecisional iff n_closed >= 20, clusters >= 8, half-width <= 0.10R. POSITIVE "
          "iff some RETEST cell (mult <= 0) has clustered 95% lb > 0 net of 0.05 ATR.")
    _emit()
    winners = []
    for mult, (s, fill, hw) in graded.items():
        decisional = s.n_closed >= _N_MIN and s.n_clusters >= _CL_MIN and hw <= _HW_BAR
        clears = decisional and s.expectancy_ci_low > 0
        if clears and mult <= 0:
            winners.append(mult)
        _emit(f"- mult {mult:+.2f}: lb={s.expectancy_ci_low:+.3f}R "
              f"(exp {s.expectancy_r:+.3f}R, fill {fill:.1%}, n={s.n_closed}, "
              f"cl={s.n_clusters}, hw={hw:.3f}R) -> "
              f"{'CLEARS lb>0' if clears else 'does not clear'}"
              f"{'' if decisional else ' [non-decisional]'}")
    if winners:
        _emit(f"\n**VERDICT: POSITIVE** -- retest cell(s) {winners} clear lb > 0 at 0.05 "
              "ATR. Evidence caps at REPLAY-SCREENED. Per the Q6 precedent this does NOT "
              "un-park continuation: promotion additionally requires a 0.10-slippage "
              "robustness re-walk and a forward variant slot, and un-parking remains a "
              "separate human-gated decision on forward evidence.")
        return "POSITIVE"
    _emit("\n**VERDICT: NULL** -- no retest cell clears clustered 95% lb > 0 net of 0.05 "
          "ATR. The retest-limit entry joins selection, timing, ordering, gates and "
          "entry-price-above-market as a falsified continuation lever; the parking rule "
          "stands and the grid is closed.")
    return "NULL"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-root", type=Path, default=Path(".cache"))
    ap.add_argument("--slip", default="0.05")
    ap.add_argument("--out", type=Path, default=Path("results_q8.md"))
    args = ap.parse_args()

    _emit("# Q8 cont_retest_entry -- negative ceiling_atr_mult (retest-limit entry)")
    _emit(f"\nPinned corpus as-of 20260703; slippage {args.slip} ATR baked into "
          "realized_r; house ticker-clustered bootstrap (seeded, canonical ticker order); "
          "continuation cohort only. Pre-registration: "
          "docs/plans/2026-08-16-retest-entry-preregistration.md")
    _emit()
    a = _load(args.cache_root, "C_retest_a", args.slip)
    b = _load(args.cache_root, "C_retest_b", args.slip)
    dd = _load(args.cache_root, "D_dump", args.slip)

    _anchor_gate(a, dd)
    _contamination(a, b)

    books: dict[float, pd.DataFrame] = {}
    for walk, variant, mult in _MULTS:
        df = a if walk == "C_retest_a" else b
        books[mult] = df[(df.variant == variant) & (df.play_type == "continuation")]

    graded = _main_table(books)
    _selection_artifact(books)
    _exit_mix(books)
    _verdict(graded)

    _emit("\n## 7. Caveats")
    _emit("\n- Replay-screened evidence tier only (pinned corpus, deterministic walk); "
          "nothing here is forward-confirmed.")
    _emit("- Only slip-0.05 dumps exist; the 0.10 robustness re-walk is conditional on "
          "POSITIVE.")
    _emit("- A retest limit is modelled as filling at `min(bar_high, ceiling)` on a bar "
          "that trades into the zone. Real retest fills also face queue position and "
          "adverse selection that OHLC replay cannot see, so a positive result here "
          "should be read as an upper bound on the mechanic.")
    _emit("- The seeded clustered bootstrap is ticker-insertion-order sensitive at the "
          "3rd decimal; rows are canonically sorted before every bound.")

    args.out.write_text("\n".join(_MD) + "\n", encoding="utf-8")
    print(f"\n[results written to {args.out}]", flush=True)  # noqa: T201


if __name__ == "__main__":
    main()

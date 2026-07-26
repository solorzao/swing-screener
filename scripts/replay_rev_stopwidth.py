"""Q7 rev_stop_width_sweep: does a wider reversal stop convert near-miss winner stopouts?

MAE post-mortem motivation: 11.3% of reversal winners see MAE >= 0.8R, so the 0.25-ATR
default stop may be shaking out trades that were about to work. Grades the pinned
queue-experiment dumps (as-of 20260703 corpus, 0.05 ATR slippage baked into
``realized_r``) for stop_buffer_atr {0.25 default, 0.35, 0.50, 0.75} on
``play_type == "reversal"``.

Walk vehicle: these books were produced by ``replay_queue_experiments.py`` walks
R_stop_a (default + stop_035) and R_stop_b (stop_050 + stop_075), sharded s0..s7 for
speed, rather than a standalone replay -- this script is analysis-only over those dumps
(read-only on the cache). D_dump (the pre-existing default-only walk) is used for the
cross-walk identity check; C_ceil_lo (fresh, carries the default as its sanity anchor)
is the same-code determinism control.

R-denominator honesty: ``realized_r`` is denominated in each width's OWN risk (wider
stop = bigger 1R), so every comparison here is per-R, not per-dollar; the main table
reports each width's median dollar risk so the denominator shift stays visible.

    python scripts/replay_rev_stopwidth.py --cache-root .cache
"""

import argparse
import glob
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pandas as pd

from swing_screener.analytics.performance import (
    breakdown,
    closed_by_ticker,
    clustered_two_sample_delta_low,
)

if TYPE_CHECKING:
    from swing_screener.db.models import PaperTrade

_ELIGIBLE_N, _ELIGIBLE_CL = 20, 8
# (variant, stop_buffer_atr, source walk)
_WIDTHS = (("default", 0.25, "R_stop_a"), ("stop_035", 0.35, "R_stop_a"),
           ("stop_050", 0.50, "R_stop_b"), ("stop_075", 0.75, "R_stop_b"))
_KEY = ["ticker", "opened_date"]


class _Row:
    """Minimal trade-like object so ``breakdown`` can grade a parquet dump row."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, _: str) -> None:
        return None


def _rows(cd: pd.DataFrame) -> "list[PaperTrade]":
    rows = [_Row(ticker=r.ticker, status=r.status, fill_status=r.fill_status,
                 realized_r=r.realized_r, variant="x") for r in cd.itertuples()]
    return cast("list[PaperTrade]", rows)


def _load_walk(root: Path, walk: str, slip: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(root / "queue_experiments" / f"{walk}_s*_slip{slip}.parquet")))
    if not files:
        raise SystemExit(f"no {walk} shards under {root}/queue_experiments")
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def _rev(df: pd.DataFrame, variant: str) -> pd.DataFrame:
    """Reversal rows for one variant, ticker-sorted so the seeded clustered bootstrap
    (ticker-insertion-order sensitive at the 3rd decimal) is reproducible."""
    out = df[(df.play_type == "reversal") & (df.variant == variant)]
    return out.sort_values(_KEY, kind="mergesort").reset_index(drop=True)


def _closed(df: pd.DataFrame) -> pd.DataFrame:
    return df[(df.status == "closed") & (df.fill_status == "filled")]


def _by_ticker(cd: pd.DataFrame) -> dict[str, list[float]]:
    d = closed_by_ticker(_rows(cd))
    return {t: d[t] for t in sorted(d)}


def _identity(label: str, left: pd.DataFrame, right: pd.DataFrame) -> None:
    """Cross-walk identity: match closed-filled reversal books on (ticker, opened_date)."""
    cl, cr = _closed(left), _closed(right)
    m = cl.merge(cr, on=_KEY, suffixes=("_l", "_r"))
    print(f"\n  {label}", flush=True)  # noqa: T201
    print(f"    closed L={len(cl)} R={len(cr)} matched={len(m)} "  # noqa: T201
          f"({len(m) / len(cl):.2%} of L, {len(m) / len(cr):.2%} of R)", flush=True)
    if not len(m):
        return
    for col in ("entry_price", "stop", "target", "realized_r"):
        agree = ((m[f"{col}_l"] - m[f"{col}_r"]).abs() < 1e-9).mean()
        print(f"    {col:<12} exact agreement on matched: {agree:.2%}", flush=True)
    print(f"    mean realized_r on matched: L={m.realized_r_l.mean():+.4f} "  # noqa: T201
          f"R={m.realized_r_r.mean():+.4f}", flush=True)


def _stat_line(label: str, width: float, cohort: pd.DataFrame) -> None:
    cd = _closed(cohort)
    n_f = int((cohort.fill_status == "filled").sum())
    n_m = int((cohort.fill_status == "missed").sum())
    fill = n_f / (n_f + n_m) if (n_f + n_m) else 0.0
    s = breakdown(_rows(cohort), "variant").get("x")
    if s is None or not s.n_closed:
        print(f"{label:<10}{width:>6.2f}{len(cohort):>10d}{fill:>7.1%}{0:>9d}{0:>9d}"  # noqa: T201
              f"{'-':>8}{'-':>9}{'-':>7}{'-':>8}{'-':>7}{'-':>10}", flush=True)
        return
    mix = cd.exit_reason.value_counts(normalize=True)
    med_risk = float(cd.risk.median())
    print(f"{label:<10}{width:>6.2f}{len(cohort):>10d}{fill:>7.1%}{s.n_closed:>9d}"  # noqa: T201
          f"{s.n_clusters:>9d}{s.expectancy_r:>+8.3f}{s.expectancy_ci_low:>+9.3f}"
          f"{mix.get('stop', 0.0):>7.1%}{mix.get('target', 0.0):>8.1%}"
          f"{mix.get('time_stop', 0.0):>7.1%}{med_risk:>10.3f}", flush=True)


def _stat_header(title: str) -> None:
    print(f"\n[{title}]", flush=True)  # noqa: T201
    print(f"{'variant':<10}{'width':>6}{'n_signal':>10}{'fill%':>7}{'n_closed':>9}"  # noqa: T201
          f"{'clusters':>9}{'exp_R':>8}{'ci_low':>9}{'stop%':>7}{'target%':>8}{'time%':>7}"
          f"{'med_risk':>10}", flush=True)


def _delta_line(label: str, cw: pd.DataFrame, cdft: pd.DataFrame) -> tuple[float, int, int]:
    """Two-sample clustered delta (width minus default). Returns (lb, n_w, cl_w)."""
    aw, ad = _by_ticker(cw), _by_ticker(cdft)
    lb = clustered_two_sample_delta_low(aw, ad)
    point = float(cw.realized_r.mean() - cdft.realized_r.mean()) if len(cw) and len(cdft) \
        else float("nan")
    print(f"{label:<10}{point:>+10.4f}{lb:>+10.3f}{len(cw):>9d}{len(aw):>9d}"  # noqa: T201
          f"{len(cdft):>9d}{len(ad):>9d}", flush=True)
    return lb, len(cw), len(aw)


def _delta_header(title: str) -> None:
    print(f"\n[{title}]", flush=True)  # noqa: T201
    print(f"{'variant':<10}{'delta_pt':>10}{'delta_lb':>10}{'n_w':>9}{'cl_w':>9}"  # noqa: T201
          f"{'n_def':>9}{'cl_def':>9}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-root", type=Path, default=Path(".cache"))
    ap.add_argument("--slip", default="0.05", help="dump slippage suffix to grade")
    args = ap.parse_args()

    walks = {w: _load_walk(args.cache_root, w, args.slip)
             for w in ("R_stop_a", "R_stop_b", "D_dump", "C_ceil_lo")}
    revs = {variant: _rev(walks[walk], variant) for variant, _, walk in _WIDTHS}

    print(f"=== Q7 rev_stop_width_sweep | slip {args.slip} | pinned corpus (as-of 20260703) "  # noqa: T201
          f"| {revs['default'].ticker.nunique()} tickers ===", flush=True)
    print("walk vehicle: replay_queue_experiments.py R_stop_a/R_stop_b, shards s0..s7",  # noqa: T201
          flush=True)

    # ---- [1] sanity: cross-walk identity --------------------------------------------
    print("\n[1] SANITY: cross-walk identity of the default reversal book "  # noqa: T201
          "(expected scale ~44k closed fills)", flush=True)
    _identity("R_stop_a default (L) vs D_dump default (R)",
              revs["default"], _rev(walks["D_dump"], "default"))
    _identity("R_stop_a default (L) vs C_ceil_lo default (R)  [same-day determinism control]",
              revs["default"], _rev(walks["C_ceil_lo"], "default"))

    # ---- [2] main table -------------------------------------------------------------
    _stat_header("2. MAIN TABLE: per stop width, play_type==reversal (full cohort)")
    for variant, width, _ in _WIDTHS:
        _stat_line(variant, width, revs[variant])

    # fill-set identity across widths (the stop knob sits below the entry floor, so the
    # fill/invalidation set barely moves; report it rather than assume it)
    base_fills = set(map(tuple, revs["default"][revs["default"].fill_status == "filled"][_KEY]
                         .itertuples(index=False)))
    for variant, _, _ in _WIDTHS[1:]:
        f = set(map(tuple, revs[variant][revs[variant].fill_status == "filled"][_KEY]
                    .itertuples(index=False)))
        print(f"  fill-set overlap {variant} vs default: {len(f & base_fills)}/{len(f)} "  # noqa: T201
              f"({len(f & base_fills) / len(f):.2%})", flush=True)

    # ---- [2b] mechanism: what default stopouts became under each width --------------
    print("\n[2b] MECHANISM (descriptive): default STOP-outs re-graded under each width",  # noqa: T201
          flush=True)
    print("     (matched closed keys; realized_r under each width is in that width's OWN R)",  # noqa: T201
          flush=True)
    print(f"{'variant':<10}{'n_matched':>10}{'->stop%':>9}{'->target%':>10}{'->time%':>9}"  # noqa: T201
          f"{'meanR_dft':>10}{'meanR_w':>9}", flush=True)
    dft_closed = _closed(revs["default"])
    for variant, _, _ in _WIDTHS[1:]:
        m = dft_closed.merge(_closed(revs[variant]), on=_KEY, suffixes=("_d", "_w"))
        ds = m[m.exit_reason_d == "stop"]
        mix = ds.exit_reason_w.value_counts(normalize=True)
        print(f"{variant:<10}{len(ds):>10d}{mix.get('stop', 0.0):>9.1%}"  # noqa: T201
              f"{mix.get('target', 0.0):>10.1%}{mix.get('time_stop', 0.0):>9.1%}"
              f"{ds.realized_r_d.mean():>+10.3f}{ds.realized_r_w.mean():>+9.3f}", flush=True)

    # ---- [3] decision deltas --------------------------------------------------------
    _delta_header("3. DELTA TABLE (decision numbers): width vs default, full reversal cohort")
    print("   two-sample per-cohort (NOT paired): per-width exits/denominators differ",  # noqa: T201
          flush=True)
    verdicts: dict[str, tuple[float, int, int]] = {}
    for variant, _, _ in _WIDTHS[1:]:
        verdicts[variant] = _delta_line(variant, _closed(revs[variant]), dft_closed)

    # ---- [4] tier cuts --------------------------------------------------------------
    for tier in ("premium", "strong"):
        sub = {v: df[df.conviction_tier == tier] for v, df in revs.items()}
        _stat_header(f"4. TIER CUT (pre-registered, descriptive): conviction_tier=={tier}")
        for variant, width, _ in _WIDTHS:
            _stat_line(variant, width, sub[variant])
        _delta_header(f"   {tier}: delta vs default (two-sample clustered)")
        dft = _closed(sub["default"])
        for variant, _, _ in _WIDTHS[1:]:
            _delta_line(variant, _closed(sub[variant]), dft)

    sub = {v: df[df.market_trend == "bear"] for v, df in revs.items()}
    _stat_header("4b. REGIME CUT (post-hoc-but-motivated, descriptive): market_trend==bear")
    for variant, width, _ in _WIDTHS:
        _stat_line(variant, width, sub[variant])
    _delta_header("   bear: delta vs default (two-sample clustered)")
    dft = _closed(sub["default"])
    for variant, _, _ in _WIDTHS[1:]:
        _delta_line(variant, _closed(sub[variant]), dft)

    # ---- [5] R-denominator honesty --------------------------------------------------
    print("\n[5] R-DENOMINATOR: realized_r is per-width own-R (wider stop = bigger 1R).",  # noqa: T201
          flush=True)
    base_risk = float(_closed(revs["default"]).risk.median())
    for variant, width, _ in _WIDTHS:
        r = float(_closed(revs[variant]).risk.median())
        print(f"  {variant:<10} width={width:.2f}  median risk ${r:.3f}/share "  # noqa: T201
              f"({r / base_risk:.2f}x default) -> comparisons are per-R, not per-dollar",
              flush=True)

    # ---- [6] pre-registered stopping rule -------------------------------------------
    print("\n[6] PRE-REGISTERED STOPPING RULE (queue doc Q7)", flush=True)  # noqa: T201
    print("  promote iff clustered 95% delta lb vs default > 0 at slip 0.05 "  # noqa: T201
          f"(n>={_ELIGIBLE_N}, clusters>={_ELIGIBLE_CL}), THEN survives a 0.10 re-walk",
          flush=True)
    any_clear = False
    for variant, (lb, n, cl) in verdicts.items():
        clears = lb > 0 and n >= _ELIGIBLE_N and cl >= _ELIGIBLE_CL
        any_clear |= clears
        tag = "CANDIDATE -> needs 0.10 slippage re-walk" if clears else "FALSIFIED at 0.05"
        print(f"  {variant:<10} delta_lb={lb:+.3f} n={n} cl={cl} -> {tag}", flush=True)  # noqa: T201
    print(f"  0.10 slippage re-walk needed: {'YES' if any_clear else 'NO'}", flush=True)  # noqa: T201


if __name__ == "__main__":
    main()

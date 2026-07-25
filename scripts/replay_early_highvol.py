"""Q2 rev_early_highvol_coherence, CELL A: is EARLY x high-vol reversal expectancy
coherent with the highvol edge, or a multiple-comparison artifact?

Grades the pinned D_dump shards (511-name corpus as-of 20260703, 0.05 ATR slippage
already baked into realized_r) with the repo's ticker-clustered bounds. The primary
cell is strength=="early" & volatility_tier=="high" on reversal rows; the four
control contrasts (early x med, early x low, confirmed x high, confirmed x med) are
REPORTED, never gated, so the gated comparison family is exactly one cell -- the
Bonferroni-corrected percentile equals the plain 2.5th.

Pre-registered pass bar (cell A only):
    clustered 95% CI lower bound > 0   (realized_r already net of 0.05 ATR)
    AND n_closed >= 20
    AND clusters >= 8
    AND CI half-width <= 0.10R  (gated on expectancy_r - clustered ci_low, the
        hardened and most conservative of the reported half-width definitions)

Cell B (the rvol-gated-book read) needs sharded detector re-runs and is out of
scope here; the verdict is "cell A PASS" / "cell A FAIL" only.

    python scripts/replay_early_highvol.py --cache-root .cache
"""

import argparse
import glob
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pandas as pd

from swing_screener.analytics.performance import breakdown

if TYPE_CHECKING:
    from swing_screener.db.models import PaperTrade

_HALF_WIDTH_BAR = 0.10
_PRIMARY = ("early", "high")
_CELLS = (
    ("early", "high"),  # primary (gated)
    ("early", "med"),  # control (reported only)
    ("early", "low"),  # control (reported only)
    ("confirmed", "high"),  # control (reported only)
    ("confirmed", "med"),  # control (reported only)
)


class _Row:
    """Minimal trade-like object so ``breakdown`` can grade a parquet dump row."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, _: str) -> None:
        return None


def _load_dump(cache_root: Path) -> pd.DataFrame:
    pattern = str(cache_root / "queue_experiments" / "D_dump_s*_slip0.05.parquet")
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"no D_dump shards match {pattern}")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    print(f"loaded {len(files)} shards | {len(df)} rows | {df.ticker.nunique()} tickers",
          flush=True)  # noqa: T201
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-root", type=Path, default=Path(".cache"),
                    help="pinned cache root holding queue_experiments/D_dump_s*.parquet")
    args = ap.parse_args()

    rev = _load_dump(args.cache_root)
    rev = rev[rev.play_type == "reversal"]
    print(f"reversal rows: {len(rev)} | {rev.ticker.nunique()} tickers\n", flush=True)  # noqa: T201

    # One breakdown call over a synthetic strength x vol_tier key: every cohort's
    # clustered bound comes from the same house primitive (breakdown -> summarize ->
    # _clustered_ci_low, cluster unit = ticker). All rows of a cohort are fed (open/
    # pending/missed included) so fill% is meaningful; summarize itself restricts the
    # R math to closed+filled rows with a realized R (NaN -> None below keeps parquet
    # NaNs out of the means).
    rows = [
        _Row(
            ticker=r.ticker,
            status=r.status,
            fill_status=r.fill_status,
            realized_r=None if pd.isna(r.realized_r) else float(r.realized_r),
            hold_bars=None if pd.isna(r.hold_bars) else float(r.hold_bars),
            cell=f"{r.strength}|{r.volatility_tier}",
        )
        for r in rev.itertuples()
    ]
    stats = breakdown(cast("list[PaperTrade]", rows), "cell")

    hdr = (f"{'cell':<18}{'role':<9}{'exp R':>8}{'ci_low':>9}{'ci_high':>9}"
           f"{'hw_low':>8}{'hw_iid':>8}{'hw_sym':>8}{'n_closed':>9}{'clusters':>9}"
           f"{'thin':>6}{'fill%':>7}")
    print(hdr, flush=True)  # noqa: T201
    results: dict[tuple[str, str], dict[str, float]] = {}
    for strength, vol in _CELLS:
        key = f"{strength}|{vol}"
        if key not in stats:
            print(f"{key:<18}(no rows)", flush=True)  # noqa: T201
            continue
        s = stats[key]
        hw_low = s.expectancy_r - s.expectancy_ci_low  # hardened (clustered) half-width
        hw_iid = 1.96 * s.expectancy_stderr  # plain IID half-width
        hw_sym = (s.expectancy_ci_high - s.expectancy_ci_low) / 2  # mixed-interval half
        role = "PRIMARY" if (strength, vol) == _PRIMARY else "control"
        print(f"{key:<18}{role:<9}{s.expectancy_r:>+8.3f}{s.expectancy_ci_low:>+9.3f}"  # noqa: T201
              f"{s.expectancy_ci_high:>+9.3f}{hw_low:>8.3f}{hw_iid:>8.3f}{hw_sym:>8.3f}"
              f"{s.n_closed:>9d}{s.n_clusters:>9d}{str(s.thin_clusters):>6}"
              f"{s.fill_rate:>7.0%}", flush=True)
        results[(strength, vol)] = {
            "exp": s.expectancy_r, "lb": s.expectancy_ci_low, "n": s.n_closed,
            "clusters": s.n_clusters, "hw_low": hw_low,
        }

    p = results[_PRIMARY]
    checks = (
        ("clustered 95% lb > 0", p["lb"] > 0, f"lb = {p['lb']:+.3f}"),
        ("n_closed >= 20", p["n"] >= 20, f"n_closed = {p['n']:.0f}"),
        ("clusters >= 8", p["clusters"] >= 8, f"clusters = {p['clusters']:.0f}"),
        (f"CI half-width <= {_HALF_WIDTH_BAR:.2f}R (exp - clustered lb)",
         p["hw_low"] <= _HALF_WIDTH_BAR, f"half-width = {p['hw_low']:.3f}R"),
    )
    print("\n[pre-registered bar, primary cell early x high]", flush=True)  # noqa: T201
    for label, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<48} {detail}", flush=True)  # noqa: T201
    verdict = "cell A PASS" if all(ok for _, ok, _ in checks) else "cell A FAIL"
    print(f"\nVERDICT: {verdict}", flush=True)  # noqa: T201
    print("(controls above are descriptive only; cell B + 0.10-slippage re-runs are"  # noqa: T201
          " still required for full CONFIRMED-COHERENT status)", flush=True)


if __name__ == "__main__":
    main()

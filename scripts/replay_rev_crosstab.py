"""Q1 rev_bear_highvol_crosstab: are the bear-regime and high-vol reversal screens the
same trades?

Grades the pinned queue-experiment dump (D_dump shards, ``play_type == "reversal"``,
0.05 ATR slippage already baked into ``realized_r``) on market_trend x volatility_tier:
marginals, the 6 trend-x-tier cells (with a Bonferroni alpha/6-corrected lower bound via
the house clustered bootstrap at the corrected quantile), a market_vol split of the
bear x high cell, and all of it repeated on the confirmed cohort. Read-only over the cache.

    python scripts/replay_rev_crosstab.py --cache-root .cache
"""

import argparse
import glob
from pathlib import Path
from statistics import NormalDist
from typing import TYPE_CHECKING, cast

import pandas as pd

from swing_screener.analytics.performance import (
    _clustered_ci_low,
    breakdown,
    closed_by_ticker,
)

if TYPE_CHECKING:
    from swing_screener.db.models import PaperTrade

# House leaderboard bound = 2.5th percentile; Bonferroni over the K=6 trend-x-tier cells.
_K_CELLS = 6
_CORR_PCT = 2.5 / _K_CELLS
_Z_CORR = NormalDist().inv_cdf(1 - _CORR_PCT / 100.0)
_ELIGIBLE_N, _ELIGIBLE_CL = 20, 8


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


def _line(label: str, cd: pd.DataFrame, *, corrected: bool = False) -> tuple[float, int, int]:
    """Print one cohort row; return (clustered lb, n_closed, n_clusters) for the verdict."""
    n_f = int((cd.fill_status == "filled").sum())
    n_m = int((cd.fill_status == "missed").sum())
    fill = n_f / (n_f + n_m) if (n_f + n_m) else 0.0
    s = breakdown(_rows(cd), "variant").get("x")
    if s is None or not s.n_closed:
        print(f"{label:<26}{'-':>8}{'-':>9}{'-':>9}{0:>9d}{0:>9d}{fill:>7.0%}", flush=True)  # noqa: T201
        return float("-inf"), 0, 0
    corr = ""
    if corrected:
        by_ticker = closed_by_ticker(_rows(cd))
        iid_corr = s.expectancy_r - _Z_CORR * s.expectancy_stderr
        lb_corr, _, thin = _clustered_ci_low(by_ticker, iid_corr, lower_pct=_CORR_PCT)
        corr = f"{lb_corr:>+9.3f}" + ("*" if thin else " ")
    print(f"{label:<26}{s.expectancy_r:>+8.3f}{s.expectancy_ci_low:>+9.3f}{corr:>10}"  # noqa: T201
          f"{s.n_closed:>9d}{s.n_clusters:>9d}{fill:>7.0%}", flush=True)
    return s.expectancy_ci_low, s.n_closed, s.n_clusters


def _header(title: str, *, corrected: bool = False) -> None:
    mid = f"{'lb_bonf6':>10}" if corrected else f"{'':>10}"
    print(f"\n[{title}]", flush=True)  # noqa: T201
    print(f"{'cohort':<26}{'exp R':>8}{'ci_low':>9}{mid}{'n_closed':>9}{'clusters':>9}"  # noqa: T201
          f"{'fill%':>7}", flush=True)


def _clears(lb: float, n: int, cl: int) -> bool:
    return lb > 0 and n >= _ELIGIBLE_N and cl >= _ELIGIBLE_CL


def _report(rev: pd.DataFrame, tag: str) -> dict[str, tuple[float, int, int]]:
    out: dict[str, tuple[float, int, int]] = {}
    _header(f"{tag}: marginals by market_trend")
    for v in ("bull", "bear", "None"):
        out[f"trend={v}"] = _line(f"trend={v}", rev[rev.market_trend.astype(str) == v])
    _header(f"{tag}: marginals by volatility_tier")
    for v in ("low", "med", "high"):
        out[f"tier={v}"] = _line(f"tier={v}", rev[rev.volatility_tier == v])
    _header(f"{tag}: market_trend x volatility_tier (K=6 Bonferroni family)", corrected=True)
    for tr in ("bull", "bear"):
        for ti in ("low", "med", "high"):
            cd = rev[(rev.market_trend == tr) & (rev.volatility_tier == ti)]
            out[f"{tr}x{ti}"] = _line(f"{tr} x {ti}", cd, corrected=True)
    bear = rev[rev.market_trend == "bear"]
    out["bearxnot-high"] = _line("bear x not-high (pooled)",
                                 bear[bear.volatility_tier.isin(["low", "med"])])
    _header(f"{tag}: bear x high split by market_vol")
    bh = bear[bear.volatility_tier == "high"]
    for v in ("calm", "elevated", "high"):
        _line(f"bear x high x {v}", bh[bh.market_vol == v])
    return out


def _verdict(res: dict[str, tuple[float, int, int]], tag: str) -> None:
    bh, bnh, gh = res["bearxhigh"], res["bearxnot-high"], res["bullxhigh"]
    print(f"\n[{tag}: pre-registered verdict]", flush=True)  # noqa: T201
    print(f"  bear x high      lb={bh[0]:+.3f} n={bh[1]} cl={bh[2]} clears={_clears(*bh)}", flush=True)  # noqa: T201
    print(f"  bear x not-high  lb={bnh[0]:+.3f} n={bnh[1]} cl={bnh[2]} clears={_clears(*bnh)}", flush=True)  # noqa: T201
    print(f"  bull x high      lb={gh[0]:+.3f} n={gh[1]} cl={gh[2]} clears={_clears(*gh)}", flush=True)  # noqa: T201
    if _clears(*bh) and not _clears(*bnh) and not _clears(*gh):
        v = "ONE EDGE COUNTED TWICE (intersection carries it)"
    elif _clears(*bnh) and _clears(*gh):
        v = "ADDITIVE (both off-diagonals clear independently)"
    else:
        v = "NEITHER pattern holds cleanly (see table)"
    print(f"  -> {v}", flush=True)  # noqa: T201
    ranked = sorted((k for k in ("trend=bear", "tier=high", "bearxhigh")
                     if _clears(*res[k])), key=lambda k: res[k][0], reverse=True)
    best = ranked[0] if ranked else "none (no eligible cohort clears lb>0)"
    print(f"  strongest single cohort for promotion: {best}", flush=True)  # noqa: T201


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-root", type=Path, default=Path(".cache"))
    ap.add_argument("--slip", default="0.05", help="D_dump slippage suffix to grade")
    args = ap.parse_args()

    files = sorted(glob.glob(str(args.cache_root / "queue_experiments"
                                 / f"D_dump_s*_slip{args.slip}.parquet")))
    if not files:
        raise SystemExit(f"no D_dump shards under {args.cache_root}")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    rev = df[df.play_type == "reversal"]
    print(f"=== rev crosstab | {len(files)} shards | slip {args.slip} | "  # noqa: T201
          f"{len(rev)} reversal rows | {rev.ticker.nunique()} tickers ===", flush=True)
    print(f"bounds: house lb = ticker-clustered 2.5th pct; lb_bonf6 = same bootstrap at the "  # noqa: T201
          f"{_CORR_PCT:.4f}th pct (alpha/6, z_iid={_Z_CORR:.3f}); '*' = thin-cluster IID fallback",
          flush=True)

    full = _report(rev, "FULL")
    conf = _report(rev[rev.strength == "confirmed"], "CONFIRMED")
    _verdict(full, "FULL")
    _verdict(conf, "CONFIRMED")


if __name__ == "__main__":
    main()

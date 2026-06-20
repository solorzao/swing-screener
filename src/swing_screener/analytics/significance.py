"""Statistical guard for the parallel-arm A/B: is an arm's edge over baseline real,
or noise on a small, optimistically-filled sample?

The three live arms (baseline / partial33_cond / partial33_chand, see
pipeline/arms.py) are the SAME underlying fills under different exit management, so
the honest comparison is a PAIRED difference in realized_r, resampled by TICKER
(trades on one name are correlated -- treating each trade as independent overstates
significance). On top we demand a MARGIN (optimistic exact-level stop/target fills
inflate every arm's R) and a Bonferroni multiple-comparisons correction across the
family of non-baseline arms. Pure, no I/O.
"""

import random
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from swing_screener.db.models import PaperTrade

PairKey = tuple[str, str, object, str]  # (ticker, timeframe, opened_date, play_type)


def _is_closed_filled(t: PaperTrade) -> bool:
    return t.status == "closed" and t.fill_status == "filled" and t.realized_r is not None


def _pair_key(t: PaperTrade) -> PairKey:
    return (t.ticker, t.timeframe, t.opened_date, t.play_type)


@dataclass(frozen=True)
class ArmVerdict:
    arm: str
    baseline: str
    n_pairs: int
    n_clusters: int          # distinct tickers (the resampling unit)
    mean_diff_r: float       # arm expectancy_r - baseline expectancy_r (pooled)
    ci_low: float
    ci_high: float
    margin_r: float
    alpha: float
    family_size: int
    verdict: str             # "insufficient_data" | "no_edge" | "winner"
    detail: str


def _paired_diffs(trades: list[PaperTrade], arm: str, baseline: str
                  ) -> dict[str, list[float]]:
    """Map ticker -> list of (arm_r - baseline_r) for every fill present in BOTH arms."""
    by_arm: dict[str, dict[PairKey, float]] = defaultdict(dict)
    for t in trades:
        if t.arm in (arm, baseline) and _is_closed_filled(t):
            assert t.realized_r is not None
            by_arm[t.arm][_pair_key(t)] = t.realized_r
    base, chal = by_arm.get(baseline, {}), by_arm.get(arm, {})
    out: dict[str, list[float]] = defaultdict(list)
    for key in base.keys() & chal.keys():
        out[key[0]].append(chal[key] - base[key])  # key[0] == ticker
    return out


def compare_arm_to_baseline(
    trades: list[PaperTrade], arm: str, *, baseline: str = "baseline",
    min_pairs: int = 30, min_clusters: int = 10, margin_r: float = 0.05,
    alpha: float = 0.05, n_boot: int = 2000, family_size: int = 1, seed: int = 0,
) -> ArmVerdict:
    """Ticker-clustered paired bootstrap of the realized_r difference (arm - baseline).

    A "winner" requires: enough paired fills (min_pairs) across enough distinct tickers
    (min_clusters), AND the Bonferroni-corrected two-sided CI lower bound to clear the
    optimistic-fill margin (ci_low > margin_r). Otherwise "no_edge"; below the data
    floor, "insufficient_data".
    """
    diffs_by_ticker = _paired_diffs(trades, arm, baseline)
    all_diffs = [d for ds in diffs_by_ticker.values() for d in ds]
    n_pairs = len(all_diffs)
    tickers = list(diffs_by_ticker)
    n_clusters = len(tickers)
    mean_diff = float(np.mean(all_diffs)) if all_diffs else 0.0

    # Bonferroni: split alpha across the family, two-sided percentile CI.
    eff_alpha = alpha / max(family_size, 1)
    lo_pct, hi_pct = 100.0 * eff_alpha / 2.0, 100.0 * (1.0 - eff_alpha / 2.0)

    if n_pairs < min_pairs or n_clusters < min_clusters:
        return ArmVerdict(arm, baseline, n_pairs, n_clusters, mean_diff, float("nan"),
                          float("nan"), margin_r, alpha, family_size, "insufficient_data",
                          f"need >={min_pairs} pairs / >={min_clusters} tickers; "
                          f"have {n_pairs}/{n_clusters}")

    rng = random.Random(seed)
    boot_means: list[float] = []
    for _ in range(n_boot):
        chosen = [rng.choice(tickers) for _ in tickers]          # resample CLUSTERS
        sample = [d for tk in chosen for d in diffs_by_ticker[tk]]
        boot_means.append(sum(sample) / len(sample))
    ci_low = float(np.percentile(boot_means, lo_pct))
    ci_high = float(np.percentile(boot_means, hi_pct))

    verdict = "winner" if ci_low > margin_r else "no_edge"
    detail = (f"{arm} vs {baseline}: {mean_diff:+.3f}R "
              f"[{ci_low:+.3f}, {ci_high:+.3f}] over {n_pairs} pairs / {n_clusters} "
              f"tickers; margin {margin_r:.3f}R, family {family_size} -> {verdict}")
    return ArmVerdict(arm, baseline, n_pairs, n_clusters, mean_diff, ci_low, ci_high,
                      margin_r, alpha, family_size, verdict, detail)


def evaluate_arms(
    trades: list[PaperTrade], arms: list[str], *, baseline: str = "baseline", **kw: object
) -> dict[str, ArmVerdict]:
    """Compare each challenger arm to baseline with family_size = number of challengers
    (the multiple-comparisons family). kw passes through to compare_arm_to_baseline."""
    kw.pop("family_size", None)  # we own it here
    return {
        arm: compare_arm_to_baseline(trades, arm, baseline=baseline,
                                     family_size=len(arms), **kw)  # type: ignore[arg-type]
        for arm in arms
    }

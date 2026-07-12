"""MAE/MFE excursions in R over the swing book's existing instrumentation.

Every stepped ``PaperTrade`` folds forward ``low_water`` (lowest low since fill,
the max ADVERSE excursion) and ``high_water`` (highest high, the max FAVOURABLE
excursion) as raw prices -- seeded to ``entry_price`` and never consumed by any
exit decision or by analytics. This module is the pure read-model that converts
those raw prices into R-multiples, so "how far underwater did winners go?" and
"how much did we leave on the table?" become answerable with no new column and no
backfill. Legacy / never-stepped rows carry None water marks and are honoured as
absent (excluded, not zero).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from statistics import fmean, median

from swing_screener.analytics.performance import _is_closed_filled
from swing_screener.db.models import PaperTrade


@dataclass(frozen=True)
class Excursion:
    """Max adverse / favourable excursion of a single trade, in R."""

    mae_r: float
    mfe_r: float


def excursion_r(trade: PaperTrade) -> Excursion | None:
    """Adverse/favourable excursion of ``trade`` in R (long convention).

    ``mae_r = (entry_price - low_water) / risk`` (how deep underwater),
    ``mfe_r = (high_water - entry_price) / risk`` (peak unrealised gain).
    Returns None when any of ``low_water``, ``high_water``, ``entry_price`` or
    ``risk`` is None, or ``risk == 0`` -- an unmeasurable row, not a zero one.
    """
    entry = trade.entry_price
    low = trade.low_water
    high = trade.high_water
    risk = trade.risk
    if entry is None or low is None or high is None or risk is None or risk == 0:
        return None
    return Excursion(mae_r=(entry - low) / risk, mfe_r=(high - entry) / risk)


def excursion_summary(trades: Iterable[PaperTrade]) -> dict[str, float]:
    """Aggregate MAE/MFE in R over closed-filled trades with a computable excursion.

    Only closed-filled rows (``_is_closed_filled``) that yield a non-None
    ``excursion_r`` contribute; everything else is skipped. An empty result
    (no trades, or none instrumented) returns all-zeros so callers never divide
    by an empty set.
    """
    maes: list[float] = []
    mfes: list[float] = []
    for t in trades:
        if not _is_closed_filled(t):
            continue
        exc = excursion_r(t)
        if exc is None:
            continue
        maes.append(exc.mae_r)
        mfes.append(exc.mfe_r)

    if not maes:
        return {
            "n": 0,
            "avg_mae_r": 0.0,
            "avg_mfe_r": 0.0,
            "median_mae_r": 0.0,
            "median_mfe_r": 0.0,
        }
    return {
        "n": len(maes),
        "avg_mae_r": fmean(maes),
        "avg_mfe_r": fmean(mfes),
        "median_mae_r": median(maes),
        "median_mfe_r": median(mfes),
    }

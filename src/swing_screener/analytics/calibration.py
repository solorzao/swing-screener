"""The conviction-calibration TEST -- the inferential half of the autonomy gate.

The Phase-2 ``pipeline.reflect.analyst_calibration`` reports DESCRIPTIVE bare means per
conviction grade (kept for the edge-file note). This module adds the INFERENTIAL test the
autonomy gate needs: does the analyst's ``high`` conviction actually OUT-EARN its ``low``
conviction, *with teeth*? Not a bare ``mean_high > mean_low`` -- a ticker-CLUSTERED
two-sample delta whose bootstrap lower bound must clear 0, gated behind an n + distinct-
ticker floor per bucket (so a thin or one-name sample can never certify, and a label-shuffle
placebo with no real signal does not pass).

Pure + deterministic: it reads already-loaded ``AnalystCall`` rows and reuses the repo's
seeded clustered two-sample primitive (``analytics.performance.clustered_two_sample_delta_low``,
the same machinery ``propose`` uses for its winner-vs-incumbent delta). No I/O, no LLM.
"""

from dataclasses import dataclass

from swing_screener.analytics.performance import (
    _CLUSTER_FLOOR,
    MIN_LEADERBOARD_N,
    clustered_two_sample_delta_low,
)
from swing_screener.db.models import AnalystCall

# The two conviction grades the test compares. ``medium`` / ``avoid`` are intentionally
# excluded: the calibration bar (per the Phase-3 design) starts at "high out-earns low",
# the cleanest separable signal, not the full avoid<low<medium<high ladder.
_HIGH = "high"
_LOW = "low"

# The HARD ceiling on the analyst's earned conviction nudge (North Star #9: the analyst's
# influence GROWS as it earns a track record -- but bounded, git-visible, in code). A play
# type that CERTIFIES (``conviction_calibrated``) earns a ±2 nudge; everything else stays at
# the default ±1. The ceiling is pinned here at 2 -- the bound can never silently widen past
# it -- and the earned step is RECOMPUTED every run from the live scored book, so it is fully
# reversible: a play type that stops calibrating drops back to ±1 on the next run.
_NUDGE_CEILING = 2


@dataclass(frozen=True)
class CalibrationVerdict:
    """The result of the conviction-calibration test (frozen; JSON-native scalars).

    ``calibrated`` is True ONLY when ``high`` out-earns ``low`` with teeth: the clustered
    two-sample delta lower bound (``ci_low``) is above 0 AND both buckets clear the n +
    distinct-ticker floors. ``high_minus_low`` is the point-estimate gap (mean high R -
    mean low R) for display. ``reason`` explains a False verdict: ``"insufficient data:
    ..."`` below the floors, or ``"high does not out-earn low (ci_low<=0)"`` when the gap's
    lower bound straddles/falls below 0."""

    calibrated: bool
    high_minus_low: float
    ci_low: float
    n_high: int
    n_low: int
    n_clusters_high: int
    n_clusters_low: int
    reason: str


def _by_ticker(calls: list[AnalystCall], conviction: str) -> dict[str, list[float]]:
    """Per-ticker realized R for the SCORED calls at ``final_conviction == conviction``.
    The ticker is the bootstrap cluster; only scored rows (``scored_at`` + ``realized_r``
    set) count."""
    out: dict[str, list[float]] = {}
    for c in calls:
        if (
            c.final_conviction == conviction
            and c.scored_at is not None
            and c.realized_r is not None
        ):
            out.setdefault(c.ticker, []).append(c.realized_r)
    return out


def _n(by_ticker: dict[str, list[float]]) -> int:
    return sum(len(v) for v in by_ticker.values())


def _mean(by_ticker: dict[str, list[float]]) -> float:
    flat = [r for rs in by_ticker.values() for r in rs]
    return sum(flat) / len(flat) if flat else 0.0


def conviction_calibrated(
    calls: list[AnalystCall],
    *,
    min_per_bucket: int = MIN_LEADERBOARD_N,
    cluster_floor: int = _CLUSTER_FLOOR,
    lower_pct: float = 5.0,
) -> CalibrationVerdict:
    """Test whether ``high`` conviction OUT-EARNS ``low`` conviction, with teeth.

    Over the SCORED ``AnalystCall``s, split realized R into the ``high`` and ``low``
    buckets (by ``final_conviction``; medium/avoid ignored). The verdict is ``calibrated``
    iff ALL hold:
      * both buckets have ``>= min_per_bucket`` scored calls;
      * both buckets span ``>= cluster_floor`` distinct tickers (enough clusters to certify
        a difference, not a one-name artifact); and
      * the ticker-CLUSTERED two-sample delta (mean high R - mean low R, resampling tickers
        independently in each bucket) has a bootstrap lower bound (at ``lower_pct``) ABOVE 0.

    Below the n / cluster floors -> ``calibrated=False, reason="insufficient data: ..."``.
    Floors met but the gap's lower bound ``<= 0`` -> ``calibrated=False, reason="high does
    not out-earn low (ci_low<=0)"``. Pure + deterministic (the clustered bootstrap is
    seeded). Reuses the same ``clustered_two_sample_delta_low`` primitive as ``propose``."""
    high = _by_ticker(calls, _HIGH)
    low = _by_ticker(calls, _LOW)
    n_high, n_low = _n(high), _n(low)
    nc_high, nc_low = len(high), len(low)
    gap = _mean(high) - _mean(low)

    shortfalls: list[str] = []
    if n_high < min_per_bucket:
        shortfalls.append(f"n_high={n_high}<{min_per_bucket}")
    if n_low < min_per_bucket:
        shortfalls.append(f"n_low={n_low}<{min_per_bucket}")
    if nc_high < cluster_floor:
        shortfalls.append(f"tickers_high={nc_high}<{cluster_floor}")
    if nc_low < cluster_floor:
        shortfalls.append(f"tickers_low={nc_low}<{cluster_floor}")
    if shortfalls:
        return CalibrationVerdict(
            calibrated=False, high_minus_low=gap, ci_low=float("-inf"),
            n_high=n_high, n_low=n_low, n_clusters_high=nc_high, n_clusters_low=nc_low,
            reason="insufficient data: " + ", ".join(shortfalls),
        )

    ci_low = clustered_two_sample_delta_low(high, low, lower_pct=lower_pct)
    if ci_low <= 0:
        return CalibrationVerdict(
            calibrated=False, high_minus_low=gap, ci_low=ci_low,
            n_high=n_high, n_low=n_low, n_clusters_high=nc_high, n_clusters_low=nc_low,
            reason="high does not out-earn low (ci_low<=0)",
        )
    return CalibrationVerdict(
        calibrated=True, high_minus_low=gap, ci_low=ci_low,
        n_high=n_high, n_low=n_low, n_clusters_high=nc_high, n_clusters_low=nc_low,
        reason="high out-earns low: clustered two-sample CI lower bound > 0",
    )


def max_conviction_step(calib: CalibrationVerdict, *, ceiling: int = _NUDGE_CEILING) -> int:
    """The EARNED conviction-nudge bound for one play type (North Star #9). PURE.

    Returns ``min(ceiling, _NUDGE_CEILING)`` (i.e. 2) when the play type's conviction is
    CALIBRATED -- the analyst has earned the wider ±2 nudge -- else ``1`` (today's hard clamp).
    The ``min`` with ``_NUDGE_CEILING`` is the discipline: even if a caller passes a larger
    ``ceiling``, the earned bound can never exceed the git-visible constant. Recomputed every
    run from the live ``CalibrationVerdict``, so it is reversible: lose calibration -> back to 1.
    """
    return min(ceiling, _NUDGE_CEILING) if calib.calibrated else 1

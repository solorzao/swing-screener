"""Drawdown over an equity curve -- the underwater view of a cumulative-R path.

Pure functions over the ``(exit_date, cum_r)`` points produced by
``analytics.performance.equity_curve``. Feed a single book's curve (filter trades by
``account`` before building the curve -- nothing here pools across books).
"""

from __future__ import annotations

from datetime import date


def drawdown_series(curve: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Underwater curve: ``running_max(cum_r) - cum_r`` at each point, always >= 0.

    Each point carries its original date; a new equity high reads 0, and the value
    grows as the curve falls below the prior peak.
    """
    out: list[tuple[date, float]] = []
    peak = float("-inf")
    for point_date, cum_r in curve:
        peak = max(peak, cum_r)
        out.append((point_date, peak - cum_r))
    return out


def max_drawdown(curve: list[tuple[date, float]]) -> float:
    """Deepest the curve ever fell below a prior peak (0.0 for an empty curve)."""
    series = drawdown_series(curve)
    if not series:
        return 0.0
    return max(dd for _, dd in series)

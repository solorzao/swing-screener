"""The Stat contract: no statistic leaves the cockpit API as a bare number.

North Star principle 2 enforced by type (docs/plans/2026-07-05-desktop-ui-design.md,
"The three mechanical rules", rule 1): every numeric the API serves travels as a
``Stat`` carrying its sample size, cluster count, both CI bounds, cost level, and
corpus id -- the frontend has NO renderer for a bare float, so a number without
provenance is unrepresentable, not merely discouraged. Where cost/corpus isn't persisted
yet, ``cost_level``/``corpus_id`` are an explicit ``None`` and the UI renders a
hollow "not measured" tick -- an honest unknown, never a guessed default.

The CI bounds are the summary's own hardened clustered bounds, mapped 1:1 from
``PerformanceSummary`` -- never recomputed here.
"""

from dataclasses import asdict, dataclass
from typing import Any

from swing_screener.analytics.performance import PerformanceSummary


@dataclass(frozen=True)
class Stat:
    """A statistic with full provenance: value + n + clusters + CI + cost + corpus."""

    value: float
    n: int
    n_clusters: int
    ci_low: float
    ci_high: float
    cost_level: str | None
    corpus_id: str | None
    facet: str
    unit: str
    thin: bool

    def as_dict(self) -> dict[str, Any]:
        """Wire form for the API: every provenance field, never just the value."""
        return asdict(self)


def stat_from_summary(
    summary: PerformanceSummary,
    *,
    cost_level: str | None,
    corpus_id: str | None,
    facet: str,
    unit: str = "R",
) -> Stat:
    """Wrap a ``PerformanceSummary``'s expectancy as a ``Stat``, 1:1.

    ``expectancy_ci_low`` is the summary's hardened (ticker-clustered) lower bound and
    ``thin_clusters`` flags its IID fallback; both pass through untouched. ``cost_level``
    and ``corpus_id`` are keyword-only with no defaults so a caller must state what it
    knows -- passing ``None`` is an explicit "not measured", not an omission.
    """
    return Stat(
        value=summary.expectancy_r,
        n=summary.n_closed,
        n_clusters=summary.n_clusters,
        ci_low=summary.expectancy_ci_low,
        ci_high=summary.expectancy_ci_high,
        cost_level=cost_level,
        corpus_id=corpus_id,
        facet=facet,
        unit=unit,
        thin=summary.thin_clusters,
    )

"""The Stat contract: no statistic leaves the cockpit API as a bare number.

North Star principle 2 enforced by type (docs/plans/2026-07-05-desktop-ui-design.md,
"The three mechanical rules", rule 1): every numeric the API serves travels as a
``Stat`` carrying its sample size, cluster count, both CI bounds, cost level, and
corpus id -- the frontend has NO renderer for a bare float, so a number without
provenance is unrepresentable, not merely discouraged. Where cost/corpus isn't persisted
yet, ``cost_level``/``corpus_id`` are an explicit ``None`` and the UI renders a
hollow "not measured" tick -- an honest unknown, never a guessed default.

``ci_low`` is the summary's hardened (ticker-clustered) lower bound; ``ci_high``
stays the IID upper -- mapped 1:1, never recomputed here.
"""

from dataclasses import asdict, dataclass

from swing_screener.analytics.performance import PairedArmDelta, PerformanceSummary


@dataclass(frozen=True, kw_only=True)
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
    thin_clusters: bool

    def as_dict(self) -> dict[str, object]:
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
        thin_clusters=summary.thin_clusters,
    )


def stat_from_paired_delta(
    pad: PairedArmDelta,
    *,
    cost_level: str | None,
    facet: str,
    unit: str = "R",
) -> Stat:
    """Wrap a ``PairedArmDelta`` as a ``Stat``, 1:1 -- the ONE home for the mapping,
    shared by the settlement engine's arm branch and the performance endpoint's arm
    rows so the two can never drift.

    ``value`` is the mean per-pair delta and ``n`` the PAIR count (both legs closed --
    smaller than either arm's own closed count); ``delta_ci_low`` is the hardened
    (ticker-clustered) lower bound with ``thin_clusters`` flagging its IID fallback,
    ``delta_ci_high`` the IID upper -- all passed through untouched. ``cost_level``
    is keyword-only with no default so the caller must state what it knows about the
    POOLED book (a paired delta reads both sides, so the stamp must cover both);
    ``corpus_id`` stays an explicit ``None`` (not persisted yet).
    """
    return Stat(
        value=pad.mean_delta,
        n=pad.n_pairs,
        n_clusters=pad.n_clusters,
        ci_low=pad.delta_ci_low,
        ci_high=pad.delta_ci_high,
        cost_level=cost_level,
        corpus_id=None,
        facet=facet,
        unit=unit,
        thin_clusters=pad.thin_clusters,
    )

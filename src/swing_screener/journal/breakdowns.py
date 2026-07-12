"""Journal breakdowns: day-of-week and hold-time slices over the swing book.

A thin aggregation layer that reuses the analytics engine. Each slice only
decides bucket MEMBERSHIP, then defers to ``analytics.performance.summarize`` so
every bucket carries the same honest clustered-CI ``PerformanceSummary`` as the
rest of the platform -- nothing here recomputes a statistic. Pure; callers filter
by ``account`` first so a breakdown never pools across books.
"""

from collections.abc import Iterable, Sequence

from swing_screener.analytics.performance import (
    PerformanceSummary,
    _rank_labels,
    breakdown,
    summarize,
)
from swing_screener.db.models import PaperTrade

# Monday-first weekday labels; the index matches ``date.weekday()`` 0..4. Trading
# exits land on weekdays, so a Sat/Sun ``exit_date`` (weekday 5/6) maps to no label
# and is skipped defensively rather than inventing a sixth bucket.
_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri")


def by_day_of_week(trades: Iterable[PaperTrade]) -> dict[str, PerformanceSummary]:
    """Group trades by the weekday of their ``exit_date`` and summarize each group.

    Labels are ``Mon``..``Fri`` and every weekday appears even when its group is
    empty. Rows without an ``exit_date`` (open/unfilled) have no weekday and are
    skipped; ``summarize`` re-applies the closed-filled filter for the stats, so a
    group only ever counts realized results. Each value is a full clustered-CI
    ``PerformanceSummary`` (reusing ``summarize`` -- this never recomputes one).
    """
    groups: dict[str, list[PaperTrade]] = {label: [] for label in _WEEKDAY_LABELS}
    for t in trades:
        if t.exit_date is None:
            continue
        weekday = t.exit_date.weekday()
        if weekday <= 4:
            groups[_WEEKDAY_LABELS[weekday]].append(t)
    return {label: summarize(group) for label, group in groups.items()}


def by_hold_time(
    trades: Iterable[PaperTrade], edges: Sequence[int] = (1, 3, 5, 10)
) -> dict[str, PerformanceSummary]:
    """Bucket trades by ``hold_bars`` into inclusive ranges and summarize each.

    Mirrors ``analytics.performance._bucket_trades_by_rank``: inclusive ranges
    ``1..edges[0]``, ``edges[0]+1..edges[1]``, ..., with a final open-ended
    ``edges[-1]+1 +`` bucket, sharing that module's ``_rank_labels`` so the label
    formatting can never drift. Every label appears even when empty. Unlike
    ``rank`` (non-null), ``hold_bars`` is nullable -- a row with no recorded hold
    (open/legacy) cannot be placed on the hold axis and is skipped.
    """
    labels = _rank_labels(edges)
    groups: dict[str, list[PaperTrade]] = {label: [] for label in labels}
    for t in trades:
        if t.hold_bars is None:
            continue
        idx = len(edges)
        for i, edge in enumerate(edges):
            if t.hold_bars <= edge:
                idx = i
                break
        groups[labels[idx]].append(t)
    return {label: summarize(group) for label, group in groups.items()}


def by_symbol(trades: Iterable[PaperTrade]) -> dict[str, PerformanceSummary]:
    """Per-symbol performance -- a thin re-export of ``breakdown(trades, "ticker")``.

    The journal's symbol slice IS the analytics ticker breakdown; this wrapper
    exists only to give the journal API one import surface. It adds nothing.
    """
    return breakdown(trades, "ticker")

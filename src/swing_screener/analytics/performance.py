"""Pure performance analytics over the shadow book of paper trades.

These functions answer screener-QC questions: is the screener good (win rate,
expectancy, profit factor, fill rate) and which factors predict winners
(breakdowns by tag, rank buckets). All functions are pure (no I/O) and safe on
empty input. The nullable ``PaperTrade`` fields (``realized_r``, ``hold_bars``,
``exit_date``) are guarded with explicit ``is not None`` checks.
"""

import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from swing_screener.db.models import PaperTrade

# 95% two-sided normal quantile for the expectancy confidence interval. A normal
# approximation (not a small-sample t), so very thin samples read optimistically --
# which is exactly why the sample size travels with the interval everywhere it's shown.
_Z95 = 1.96

# Below this many closed trades a variant's expectancy is too thin to trust. The leaderboards
# (dashboard + replay CLI) rank trusted variants above thin ones, then by the lower CI bound.
MIN_LEADERBOARD_N = 20


@dataclass(frozen=True)
class PerformanceSummary:
    """Aggregate screener-QC stats over a set of paper trades."""

    n_total: int
    n_filled: int
    fill_rate: float
    n_closed: int
    win_rate: float
    expectancy_r: float
    profit_factor: float
    avg_hold_bars: float
    # Confidence on the expectancy estimate: the standard error of the mean R and its
    # 95% interval. With < 2 closed trades the std is undefined, so stderr is 0 and the
    # interval collapses to the point estimate -- thin samples are flagged by n_closed,
    # never crowned. Used to rank variants by a LOWER bound so noise can't win.
    expectancy_stderr: float
    expectancy_ci_low: float
    expectancy_ci_high: float


def _is_filled(t: PaperTrade) -> bool:
    return t.fill_status == "filled"


def _is_closed_filled(t: PaperTrade) -> bool:
    """A realized result: a filled trade that has closed with an R-multiple."""
    return t.status == "closed" and t.fill_status == "filled" and t.realized_r is not None


def summarize(trades: Iterable[PaperTrade]) -> PerformanceSummary:
    """Compute aggregate stats over ``trades``. Empty input yields all zeros."""
    trades = list(trades)
    n_total = len(trades)
    n_filled = sum(1 for t in trades if _is_filled(t))
    fill_rate = n_filled / n_total if n_total else 0.0

    closed = [t for t in trades if _is_closed_filled(t)]
    n_closed = len(closed)

    realized = [t.realized_r for t in closed if t.realized_r is not None]
    wins = [r for r in realized if r > 0]
    losses = [r for r in realized if r < 0]

    win_rate = len(wins) / n_closed if n_closed else 0.0
    expectancy_r = sum(realized) / n_closed if n_closed else 0.0

    # Standard error of the mean R + its 95% interval. Sample stdev (ddof=1) needs >= 2
    # points; with fewer the edge is unmeasurable, so stderr is 0 and the CI collapses to
    # the point estimate (n_closed is what flags it as untrustworthy downstream).
    if n_closed >= 2:
        expectancy_stderr = statistics.stdev(realized) / (n_closed ** 0.5)
    else:
        expectancy_stderr = 0.0
    expectancy_ci_low = expectancy_r - _Z95 * expectancy_stderr
    expectancy_ci_high = expectancy_r + _Z95 * expectancy_stderr

    if not n_closed:
        profit_factor = 0.0
    elif losses:
        profit_factor = sum(wins) / abs(sum(losses))
    elif wins:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    holds = [t.hold_bars for t in closed if t.hold_bars is not None]
    avg_hold_bars = sum(holds) / len(holds) if holds else 0.0

    return PerformanceSummary(
        n_total=n_total,
        n_filled=n_filled,
        fill_rate=fill_rate,
        n_closed=n_closed,
        win_rate=win_rate,
        expectancy_r=expectancy_r,
        profit_factor=profit_factor,
        avg_hold_bars=avg_hold_bars,
        expectancy_stderr=expectancy_stderr,
        expectancy_ci_low=expectancy_ci_low,
        expectancy_ci_high=expectancy_ci_high,
    )


def breakdown(trades: Iterable[PaperTrade], key: str) -> dict[str, PerformanceSummary]:
    """Group trades by ``str(getattr(t, key))`` and summarize each group."""
    groups: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in trades:
        groups[str(getattr(t, key))].append(t)
    return {k: summarize(v) for k, v in groups.items()}


def _rank_labels(edges: Sequence[int]) -> list[str]:
    """Build inclusive bucket labels from ascending ``edges``.

    edges ``[2]``      -> ["1-2", "3+"]
    edges ``[5, 10]``  -> ["1-5", "6-10", "11+"]
    """
    labels: list[str] = []
    low = 1
    for edge in edges:
        labels.append(f"{low}-{edge}")
        low = edge + 1
    labels.append(f"{low}+")
    return labels


def rank_bucket(
    trades: Iterable[PaperTrade], edges: Sequence[int]
) -> dict[str, PerformanceSummary]:
    """Bucket trades by ``rank`` into ranges defined by ``edges`` and summarize each.

    Buckets are inclusive ranges ``1..edges[0]``, ``edges[0]+1..edges[1]``, ...,
    with a final open-ended ``edges[-1]+1 +`` bucket. Every bucket label appears
    in the result even when it has no trades.
    """
    labels = _rank_labels(edges)
    groups: dict[str, list[PaperTrade]] = {label: [] for label in labels}
    for t in trades:
        idx = len(edges)
        for i, edge in enumerate(edges):
            if t.rank <= edge:
                idx = i
                break
        groups[labels[idx]].append(t)
    return {label: summarize(groups[label]) for label in labels}


def equity_curve(trades: Iterable[PaperTrade]) -> list[tuple[date, float]]:
    """Cumulative realized R over closed-filled trades, ordered by ``exit_date``.

    Trades without an ``exit_date`` are skipped. Returns ``(exit_date, cum_r)``
    points in ascending date order.
    """
    points: list[tuple[date, float]] = [
        (t.exit_date, t.realized_r)
        for t in trades
        if _is_closed_filled(t) and t.exit_date is not None and t.realized_r is not None
    ]
    points.sort(key=lambda p: p[0])

    curve: list[tuple[date, float]] = []
    cum = 0.0
    for exit_date, realized_r in points:
        cum += realized_r
        curve.append((exit_date, cum))
    return curve

"""Pure performance analytics over the shadow book of paper trades.

These functions answer screener-QC questions: is the screener good (win rate,
expectancy, profit factor, fill rate) and which factors predict winners
(breakdowns by tag, rank buckets). All functions are pure (no I/O) and safe on
empty input. The nullable ``PaperTrade`` fields (``realized_r``, ``hold_bars``,
``exit_date``) are guarded with explicit ``is not None`` checks.
"""

import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from swing_screener.db.models import PaperTrade

# 95% two-sided normal quantile for the expectancy confidence interval. A normal
# approximation (not a small-sample t), so very thin samples read optimistically --
# which is exactly why the sample size travels with the interval everywhere it's shown.
_Z95 = 1.96

# Below this many closed trades a variant's expectancy is too thin to trust. The leaderboards
# (dashboard + replay CLI) rank trusted variants above thin ones, then by the lower CI bound.
MIN_LEADERBOARD_N = 20

# Ticker-clustered bootstrap for the expectancy lower bound. Trades concentrate on a few
# tickers and overlap in time, so the IID normal approximation treats correlated trades as
# independent and UNDERSTATES uncertainty. Resampling whole tickers (clusters) respects the
# within-ticker correlation and reads honestly. Below _CLUSTER_FLOOR distinct tickers there
# are too few clusters to bootstrap, so the bound falls back to IID (flagged thin).
_CLUSTER_FLOOR = 8
_N_BOOT = 1000
_BOOT_SEED = 12345


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
    #
    # ``expectancy_ci_low`` is the HARDENED bound: a ticker-clustered bootstrap (resampling
    # whole tickers) when there are enough distinct tickers, otherwise the IID bound. It is
    # never more optimistic than the IID bound. ``n_clusters`` is the distinct-ticker count;
    # ``thin_clusters`` flags that the clustered bootstrap couldn't run (too few tickers) and
    # the bound fell back to IID. ``expectancy_ci_high`` stays the IID upper bound -- every
    # gate keys off the lower bound, so only that one is hardened.
    expectancy_stderr: float
    expectancy_ci_low: float
    expectancy_ci_high: float
    n_clusters: int
    thin_clusters: bool


def _is_filled(t: PaperTrade) -> bool:
    return t.fill_status == "filled"


def _is_closed_filled(t: PaperTrade) -> bool:
    """A realized result: a filled trade that has closed with an R-multiple."""
    return t.status == "closed" and t.fill_status == "filled" and t.realized_r is not None


def _clustered_ci_low(
    by_ticker: dict[str, list[float]], iid_low: float, *, lower_pct: float = 2.5
) -> tuple[float, int, bool]:
    """Ticker-clustered bootstrap lower bound on mean R at the ``lower_pct`` percentile.
    Resample TICKERS with replacement (respecting within-ticker correlation), pool their
    trades, take the mean; the ``lower_pct``-th percentile of those means is the lower bound.
    ``lower_pct`` defaults to 2.5 (the fixed leaderboard bound); a stricter/deeper percentile
    (e.g. multiple-comparisons-corrected) yields a lower, more conservative bound. Returns
    min(iid_low, clustered_low) so it can never read MORE optimistic than the IID bound;
    below the distinct-ticker floor it falls back to iid_low flagged thin."""
    tickers = list(by_ticker)
    n_clusters = len(tickers)
    if n_clusters < _CLUSTER_FLOOR:
        return iid_low, n_clusters, True
    # Reseed per call (NOT a shared/module rng) so each breakdown group's bound is
    # reproducible independent of call order -- a shared rng would make a group's CI
    # depend on how many groups ran before it. Do not "optimize" this to module scope.
    rng = np.random.default_rng(_BOOT_SEED)
    pools = [np.asarray(by_ticker[t], dtype=float) for t in tickers]
    idx = np.arange(n_clusters)
    means = np.empty(_N_BOOT)
    for b in range(_N_BOOT):
        pick = rng.choice(idx, size=n_clusters, replace=True)
        means[b] = np.concatenate([pools[i] for i in pick]).mean()
    clustered_low = float(np.percentile(means, lower_pct))
    return min(iid_low, clustered_low), n_clusters, False


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
    expectancy_ci_high = expectancy_r + _Z95 * expectancy_stderr

    # Harden the LOWER bound with a ticker-clustered bootstrap. The IID bound assumes every
    # trade is independent; in reality trades cluster on a few tickers, so resampling whole
    # tickers reads honestly and never more optimistically than IID. Below the distinct-ticker
    # floor (or with < 2 closed trades) the bootstrap can't run and it falls back to IID, thin.
    by_ticker: dict[str, list[float]] = defaultdict(list)
    for t in closed:
        if t.realized_r is not None:
            by_ticker[t.ticker].append(t.realized_r)
    iid_low = expectancy_r - _Z95 * expectancy_stderr
    if n_closed >= 2:
        expectancy_ci_low, n_clusters, thin_clusters = _clustered_ci_low(by_ticker, iid_low)
    else:
        expectancy_ci_low, n_clusters, thin_clusters = iid_low, len(by_ticker), True

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
        n_clusters=n_clusters,
        thin_clusters=thin_clusters,
    )


def breakdown(trades: Iterable[PaperTrade], key: str) -> dict[str, PerformanceSummary]:
    """Group trades by ``str(getattr(t, key))`` and summarize each group."""
    groups: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in trades:
        groups[str(getattr(t, key))].append(t)
    return {k: summarize(v) for k, v in groups.items()}


def leaderboard_order(
    summaries: Mapping[str, PerformanceSummary], *, min_n: int = MIN_LEADERBOARD_N
) -> list[str]:
    """Names best-first for every leaderboard: trusted samples (``n_closed >= min_n``) above
    thin ones, then by the lower 95% expectancy bound within each tier.

    The two-tier key is load-bearing: a 1-trade sample has no computable interval (its CI
    collapses to the point estimate), so ranking by the lower bound ALONE would let a lone
    lucky trade top a deep, steady config. Sorting trusted-first defeats that.
    """
    def _key(name: str) -> tuple[bool, float]:
        s = summaries[name]
        return (s.n_closed >= min_n, s.expectancy_ci_low)

    return sorted(summaries, key=_key, reverse=True)


def leaderboard_flag(s: PerformanceSummary) -> str:
    """Trust flag for a leaderboard row -- the single source of truth shared by the replay
    CLI table and the dashboard. ``iid`` wins over ``thin``/``ok``: when the clustered
    bootstrap couldn't run (fewer than the distinct-ticker floor), ``expectancy_ci_low`` is
    the weaker IID-fallback bound, which the reader needs to see over the sample-size flag."""
    if s.thin_clusters:
        return "iid"
    return "thin" if s.n_closed < MIN_LEADERBOARD_N else "ok"


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


def _score_labels(edges: Sequence[float]) -> list[str]:
    """Band labels from ascending score ``edges`` in (0, 1).

    edges ``[0.5, 0.7]`` -> ["0.00-0.50", "0.50-0.70", "0.70-1.00"]. Bands are
    lower-inclusive / upper-exclusive; the last runs to 1.00 inclusive.
    """
    labels: list[str] = []
    low = 0.0
    for edge in edges:
        labels.append(f"{low:.2f}-{edge:.2f}")
        low = edge
    labels.append(f"{low:.2f}-1.00")
    return labels


def score_bucket(
    trades: Iterable[PaperTrade], edges: Sequence[float]
) -> dict[str, PerformanceSummary]:
    """Bucket trades by ``signal_score`` into bands defined by ``edges`` and summarize each.

    The calibration check: a predictive score makes ``expectancy_r`` trend UP across the
    bands (high-score setups should out-earn low-score ones). A flat or inverted trend
    means the score isn't separating winners from losers. Every band label appears even
    when empty; a score exactly on an edge falls into the higher band (lower-inclusive).
    """
    labels = _score_labels(edges)
    groups: dict[str, list[PaperTrade]] = {label: [] for label in labels}
    for t in trades:
        idx = len(edges)
        for i, edge in enumerate(edges):
            if t.signal_score < edge:
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

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

# Conviction sizing (item 1b): weight each closed trade's R by its conviction_tier when
# computing a SIZE-WEIGHTED expectancy -- deploy more capital to the edge cohort, less to the
# dead baseline. Risk-equal weighting (all 1.0) recovers the plain mean.
_DEFAULT_CONVICTION_WEIGHTS = {"premium": 2.0, "strong": 1.0, "base": 0.5}

# conviction_tier was only COMPUTED from this date: the a7c2e9f4d8b6 migration backfilled
# every earlier row with the literal lowest tier "base", so ~683 unknown-tier fills would
# masquerade as deliberate base-tier picks in any tier-conditioned statistic (2026-07
# audit). Tier-conditioned aggregates exclude rows opened before it; an undated row
# (tests/fakes -- production always stamps opened_date) fails open.
TIER_STAMPED_FROM = date(2026, 6, 29)


def _tier_stamped(trades: Iterable[PaperTrade]) -> list[PaperTrade]:
    """Only trades whose ``conviction_tier`` was actually computed (see TIER_STAMPED_FROM)."""
    return [t for t in trades
            if t.opened_date is None or t.opened_date >= TIER_STAMPED_FROM]


# fill_slippage_atr's default flipped 0.0 -> 0.05 on 2026-07-02 (0e3a1e8, PR #75: the
# net-of-cost book fix of the 2026-07 audit). realized_r is baked net-at-exit, so rows
# exited BEFORE this date are gross of costs with no per-row marker distinguishing them.
COST_STAMPED_FROM = date(2026, 7, 2)


def cost_level_for(trades: Iterable[PaperTrade]) -> str | None:
    """The slippage level a cohort's realized R provably carries: ``"0.05"`` iff there is
    at least one closed-filled trade and EVERY closed-filled trade exited on/after
    ``COST_STAMPED_FROM``; otherwise ``None``.

    Honesty rules, do not weaken:

    - The book is mixed gross/net: slippage is applied AT EXIT, and rows exited before
      the 0.05 default shipped realized gross R with no per-row marker. The only honest
      aggregate stamp is "every trade in this cohort provably exited after the cutoff",
      so ONE pre-cutoff exit (or an unprovable one) poisons the whole cohort to ``None``.
    - A closed row with ``exit_date is None`` cannot prove its cost level -> ``None``.
    - Even ``"0.05"`` means level-exits-only: momentum_flip/time_stop exits use the bar
      close and are never haircut, so "net @0.05" must not be overclaimed in tooltips.

    Open/unfilled rows carry no realized cost yet and neither earn nor block the stamp.
    """
    closed = [t for t in trades if _is_closed_filled(t)]
    if not closed:
        return None
    if all(t.exit_date is not None and t.exit_date >= COST_STAMPED_FROM for t in closed):
        return "0.05"
    return None


# signal_score's DEFINITION changed for the reversal book on 2026-07-03 (score v2:
# confirmation-lag + volume weights replaced the falsified legacy vector -- see
# docs/plans/2026-07-03-reversal-score-overhaul.md). FORWARD rows scored before then
# measure a different quantity, so a score-band aggregate over the forward book must
# exclude them or one bucket pools two definitions and the resulting verdicts steer
# conviction with a biased number. Keyed per play type: only reversal's score changed.
SCORE_STAMPED_FROM: dict[str, date] = {"reversal": date(2026, 7, 3)}


def score_stamped(trades: Iterable[PaperTrade]) -> list[PaperTrade]:
    """Only trades whose ``signal_score`` was computed under the CURRENT definition
    (see SCORE_STAMPED_FROM).

    For FORWARD-book score aggregates only: a forward row's score was frozen at booking
    time by whatever code ran that night. Do NOT apply this to a REPLAY book -- a replay
    walk scores every row with the current code, so its (historical) ``opened_date``
    says nothing about score vintage and the filter would wrongly empty it. Undated
    rows (tests/fakes) fail open, mirroring ``_tier_stamped``."""
    out: list[PaperTrade] = []
    for t in trades:
        cutoff = SCORE_STAMPED_FROM.get(t.play_type)
        if cutoff is None or t.opened_date is None or t.opened_date >= cutoff:
            out.append(t)
    return out


def size_weighted_expectancy(
    trades: Iterable[PaperTrade], weights: Mapping[str, float] | None = None
) -> tuple[float, float]:
    """Conviction-weighted mean realized R over CLOSED trades, plus the total weight deployed.

    Each trade's R is weighted by ``weights[conviction_tier]`` (default: premium 2x, strong 1x,
    base 0.5x). Inherently tier-conditioned, so pre-stamping legacy rows are excluded (see
    ``TIER_STAMPED_FROM``). Pure; safe on empty input (returns (0.0, 0.0)). With uniform
    weights this equals the plain expectancy over the stamped cohort, so a gain over that
    plain expectancy measures the sizing edge."""
    w = weights or _DEFAULT_CONVICTION_WEIGHTS
    num = den = 0.0
    for t in _tier_stamped(trades):
        if t.status == "closed" and t.realized_r is not None:
            wt = w.get(t.conviction_tier, 1.0)
            num += wt * float(t.realized_r)
            den += wt
    return (num / den if den else 0.0), den


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


def clustered_two_sample_delta_low(
    a_by_ticker: Mapping[str, Sequence[float]],
    b_by_ticker: Mapping[str, Sequence[float]],
    *,
    seed: int = _BOOT_SEED,
    n_boot: int = _N_BOOT,
    lower_pct: float = 2.5,
) -> float:
    """Lower ``lower_pct``-percentile bound on ``mean(a) - mean(b)``, resampling TICKERS
    with replacement INDEPENDENTLY in each book (a two-sample clustered bootstrap, NOT
    paired -- the two books are not the same sample).

    The repo's honest two-sample primitive: it respects within-ticker correlation by
    resampling whole clusters, so an edge concentrated on a couple of correlated names
    cannot read as significant. Returns ``-inf`` if either book is empty (cannot certify).
    Seeded -> deterministic. Shared by ``propose`` (config deltas, ticker-keyed R per
    variant) and the conviction-calibration test (high vs low R per ticker)."""
    a = {k: list(v) for k, v in a_by_ticker.items() if v}
    b = {k: list(v) for k, v in b_by_ticker.items() if v}
    if not a or not b:
        return float("-inf")
    rng = np.random.default_rng(seed)
    at, bt = list(a), list(b)
    deltas = np.empty(n_boot)
    for i in range(n_boot):
        am = np.concatenate([a[at[k]] for k in rng.integers(0, len(at), len(at))]).mean()
        bm = np.concatenate([b[bt[k]] for k in rng.integers(0, len(bt), len(bt))]).mean()
        deltas[i] = am - bm
    return float(np.percentile(deltas, lower_pct))


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


@dataclass(frozen=True)
class PairedArmDelta:
    """A same-sample arm A/B: the mean PER-PAIR R difference (arm minus baseline) with a
    ticker-clustered lower bound. Arms duplicate every fill (identical entry economics),
    so pairing on the fill identity removes the entry noise a two-sample comparison
    keeps -- the honest way to judge an exit-policy arm. ``thin_clusters`` flags that the
    clustered bootstrap could not run and the bound fell back to IID.

    Mirroring ``PerformanceSummary``: only the LOWER bound is hardened by the clustered
    bootstrap (every retire/promote gate keys off it); ``delta_ci_high`` stays the plain
    IID normal approximation (``mean_delta + 1.96 * stderr``) and consumers label it so
    (the settlement card's futility check reads it as 'iid'). ``stderr`` is the raw IID
    standard error of the mean delta; 0.0 with fewer than 2 pairs (the interval
    collapses to the point -- ``n_pairs`` is what flags it as untrustworthy)."""

    n_pairs: int
    mean_delta: float
    stderr: float
    delta_ci_low: float
    delta_ci_high: float
    n_clusters: int
    thin_clusters: bool


def paired_arm_delta(
    trades: Iterable[PaperTrade], arm: str, baseline: str = "baseline"
) -> PairedArmDelta:
    """Pair each of ``arm``'s closed fills with its ``baseline`` twin and bound the mean
    R difference (arm minus baseline), ticker-clustered.

    The pair identity is ``(ticker, timeframe, play_type, variant, trigger_ts)`` -- 1:1
    by construction (``open_from_signals`` writes every arm's row from the same
    candidate). Only pairs where BOTH legs are closed-and-filled count (a pair with one
    leg still open has no realized delta yet); rows without a ``trigger_ts``
    (legacy/tests) carry no pair identity and are skipped. Until this existed, every arm
    decision (flip retirement, partial bake-off) was read off bare per-arm expectancy
    tables -- undecidable under the repo's own CI rules."""
    trades = list(trades)

    def _key(t: PaperTrade) -> tuple[str, str, str, str, object]:
        return (t.ticker, t.timeframe, t.play_type, t.variant, t.trigger_ts)

    arm_r = {_key(t): float(t.realized_r) for t in trades
             if t.arm == arm and t.trigger_ts is not None and _is_closed_filled(t)
             and t.realized_r is not None}
    deltas: list[float] = []
    deltas_by_ticker: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if (t.arm != baseline or t.trigger_ts is None or not _is_closed_filled(t)
                or t.realized_r is None):
            continue
        other = arm_r.get(_key(t))
        if other is None:
            continue
        d = other - float(t.realized_r)
        deltas.append(d)
        deltas_by_ticker[t.ticker].append(d)

    n = len(deltas)
    if n == 0:
        return PairedArmDelta(n_pairs=0, mean_delta=0.0, stderr=0.0, delta_ci_low=0.0,
                              delta_ci_high=0.0, n_clusters=0, thin_clusters=True)
    mean = sum(deltas) / n
    stderr = statistics.stdev(deltas) / (n ** 0.5) if n >= 2 else 0.0
    iid_low = mean - _Z95 * stderr
    low, n_clusters, thin = _clustered_ci_low(deltas_by_ticker, iid_low)
    return PairedArmDelta(n_pairs=n, mean_delta=mean, stderr=stderr, delta_ci_low=low,
                          delta_ci_high=mean + _Z95 * stderr, n_clusters=n_clusters,
                          thin_clusters=thin)


def breakdown(trades: Iterable[PaperTrade], key: str) -> dict[str, PerformanceSummary]:
    """Group trades by ``str(getattr(t, key))`` and summarize each group.

    Slicing by ``conviction_tier`` excludes pre-stamping legacy rows (see
    ``TIER_STAMPED_FROM``): their 'base' is a migration backfill, not a computed tier,
    and pooling them poisons the tier ladder. Every other key is untouched."""
    if key == "conviction_tier":
        trades = _tier_stamped(trades)
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


def _bucket_trades_by_score(
    trades: Iterable[PaperTrade], edges: Sequence[float]
) -> dict[str, list[PaperTrade]]:
    """Group trades into score bands (lower-inclusive: a score on an edge falls in the
    higher band). The single source of the score-band MEMBERSHIP rule, shared by
    score_bucket (which summarizes each group) and the reflection grader (which needs the
    raw trade lists)."""
    labels = _score_labels(edges)
    groups: dict[str, list[PaperTrade]] = {label: [] for label in labels}
    for t in trades:
        idx = len(edges)
        for i, edge in enumerate(edges):
            if t.signal_score < edge:
                idx = i
                break
        groups[labels[idx]].append(t)
    return groups


def score_bucket(
    trades: Iterable[PaperTrade], edges: Sequence[float]
) -> dict[str, PerformanceSummary]:
    """Bucket trades by ``signal_score`` into bands defined by ``edges`` and summarize each.

    The calibration check: a predictive score makes ``expectancy_r`` trend UP across the
    bands (high-score setups should out-earn low-score ones). A flat or inverted trend
    means the score isn't separating winners from losers. Every band label appears even
    when empty; a score exactly on an edge falls into the higher band (lower-inclusive).
    """
    return {
        label: summarize(group)
        for label, group in _bucket_trades_by_score(trades, edges).items()
    }


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


def trailing_expectancy(
    trades: Iterable[PaperTrade], *, window: int = 20
) -> list[tuple[date, float]]:
    """Rolling mean realized R over the trailing ``window`` closes, ordered by
    ``exit_date`` -- the settlement cards' sparkline of whether the edge is drifting.

    Same filtering/ordering as ``equity_curve``: closed-filled trades with an
    ``exit_date``, ascending. One value is computed per closing trade (with fewer than
    ``window`` closes so far, the mean of what exists), then same-date closes collapse
    to a single point carrying the LAST value of that date. Bootstrap-free point
    estimates only: a card grid calling the 1000-draw bootstrap per point would be
    ruinous, and the sparkline shows drift, not certification -- gates keep reading the
    hardened CI bounds, never this curve.
    """
    points: list[tuple[date, float]] = [
        (t.exit_date, t.realized_r)
        for t in trades
        if _is_closed_filled(t) and t.exit_date is not None and t.realized_r is not None
    ]
    points.sort(key=lambda p: p[0])

    curve: list[tuple[date, float]] = []
    values: list[float] = []
    for exit_date, realized_r in points:
        values.append(realized_r)
        tail = values[-window:]
        point = (exit_date, sum(tail) / len(tail))
        if curve and curve[-1][0] == exit_date:
            curve[-1] = point
        else:
            curve.append(point)
    return curve

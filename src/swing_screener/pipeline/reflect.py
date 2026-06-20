"""The deterministic GRADER -- the North Star ("evidence over narrative") in code.

Every verdict is stamped by code, never an LLM: each pre-registered bucket gets a
multiple-comparisons-corrected lower bound on its net-of-cost expectancy, and the
bucket is tiered by which book (the forward shadow book or the replay corpus) clears
that bound. The family of hypotheses is FROZEN below; adding a dimension is a
deliberate, git-visible change that resets the Bonferroni denominator K -- you cannot
quietly widen the search and keep the same confidence.

The grader is PURE: it takes already-loaded trade lists (the forward book via the repo,
the replay corpus via ``pipeline.replay``) and returns ``Verdict`` rows. Falsification of
prior claims against the previous edge file lives in a later task, not here.
"""

import statistics
from dataclasses import dataclass

from swing_screener.analytics.performance import (
    _CLUSTER_FLOOR,
    MIN_LEADERBOARD_N,
    _clustered_ci_low,
    _score_labels,
    summarize,
)
from swing_screener.db.models import PaperTrade

# PRE-REGISTERED univariate family (frozen; adding a dimension is a deliberate git-visible
# change that resets K). Categorical dims enumerate buckets; "score" uses score_bucket edges.
_SCORE_EDGES = (0.5, 0.6, 0.7, 0.8)
_FAMILY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("market_trend", ("bull", "bear")),
    ("volatility_tier", ("low", "med", "high")),
    ("score", ()),  # buckets are the score_bucket band labels (handled specially)
)
_ALPHA = 0.05  # family-wise; one-sided Bonferroni per bucket
_MARGIN_R = 0.0  # net-of-cost edge must clear this (replay is haircut upstream)


def _family_size() -> int:
    """K = total buckets across the family (the Bonferroni denominator)."""
    n = 0
    for dim, buckets in _FAMILY:
        n += len(_SCORE_EDGES) + 1 if dim == "score" else len(buckets)
    return n


@dataclass(frozen=True)
class Verdict:
    """One graded (dimension, bucket) cell for a play type. ``ci_low`` is the
    multiple-comparisons-corrected effective lower bound on whichever ``source`` decided
    the tier; the verdict is emitted even for hunches so the edge file can show what's
    being watched."""

    play_type: str
    dimension: str  # "market_trend" | "volatility_tier" | "score"
    bucket: str  # e.g. "bull", "high", "0.70-0.80"
    tier: str  # "forward_confirmed" | "replay_screened" | "hunch"
    n: int  # n_closed of the deciding source (or the better of the two)
    expectancy_r: float
    ci_low: float  # MC-corrected effective lower bound on the deciding source
    n_clusters: int
    source: str  # "forward" | "replay" | "none"


def _bucket_bound(trades: list[PaperTrade], k: int) -> tuple[float, int, int, float, bool]:
    """Return (effective_lower_bound, n_closed, n_clusters, expectancy_r, thin) for one
    bucket's trades, at the Bonferroni-corrected one-sided level alpha/K. Reuses summarize
    for the point/stderr/cluster-count; computes the corrected iid bound and the corrected
    clustered bound, taking the min (never more optimistic than iid)."""
    s = summarize(trades)
    if s.n_closed == 0:
        return float("-inf"), 0, 0, 0.0, True
    alpha_c = _ALPHA / max(k, 1)
    z = statistics.NormalDist().inv_cdf(1.0 - alpha_c)  # one-sided
    iid_corr = s.expectancy_r - z * s.expectancy_stderr
    by_ticker: dict[str, list[float]] = {}
    for t in trades:
        if t.realized_r is not None:
            by_ticker.setdefault(t.ticker, []).append(t.realized_r)
    eff_low, n_clusters, thin = _clustered_ci_low(by_ticker, iid_corr, lower_pct=100.0 * alpha_c)
    return eff_low, s.n_closed, n_clusters, s.expectancy_r, thin


def _confirms(eff_low: float, n_closed: int, n_clusters: int, thin: bool) -> bool:
    """The three-part gate every tier above ``hunch`` must clear: the corrected bound beats
    the cost margin, the sample is deep enough to trust, and the clustered bootstrap actually
    ran (>= the distinct-ticker floor, i.e. not thin)."""
    return (
        eff_low > _MARGIN_R
        and n_closed >= MIN_LEADERBOARD_N
        and not thin
        and n_clusters >= _CLUSTER_FLOOR
    )


def _bucketed(trades: list[PaperTrade], dimension: str) -> dict[str, list[PaperTrade]]:
    """Slice ``trades`` into the family's pre-registered buckets for one ``dimension``.
    Categorical dims filter by ``getattr(t, dimension) == bucket``; the "score" dim keys
    off ``score_bucket``'s band labels (lower-inclusive / upper-exclusive) so the verdict
    bucket names match the calibration table exactly."""
    if dimension == "score":
        labels = _score_labels(_SCORE_EDGES)
        groups: dict[str, list[PaperTrade]] = {label: [] for label in labels}
        for t in trades:
            idx = len(_SCORE_EDGES)
            for i, edge in enumerate(_SCORE_EDGES):
                if t.signal_score < edge:
                    idx = i
                    break
            groups[labels[idx]].append(t)
        return groups
    buckets = next(b for d, b in _FAMILY if d == dimension)
    return {b: [t for t in trades if getattr(t, dimension) == b] for b in buckets}


def _buckets_for(dimension: str, buckets: tuple[str, ...]) -> tuple[str, ...]:
    """The ordered bucket labels for a dimension (score expands to its band labels)."""
    if dimension == "score":
        return tuple(_score_labels(_SCORE_EDGES))
    return buckets


def grade(
    play_type: str,
    forward_trades: list[PaperTrade],
    replay_trades: list[PaperTrade],
) -> list[Verdict]:
    """Grade every pre-registered (dimension, bucket) cell for ``play_type``.

    For each cell, slice the matching trades from BOTH books and compute the
    Bonferroni-corrected lower bound (``k = _family_size()``). The tier is:
      * ``forward_confirmed`` if the FORWARD bound clears the three-part gate (source forward),
      * else ``replay_screened`` if the REPLAY bound clears the SAME gate (source replay),
      * else ``hunch`` (source none), carrying whichever book has data for display.
    One verdict per cell -- including empty/hunch cells -- so the edge file can show the
    full watchlist. Deterministic: no LLM, seeded clustered bootstrap.
    """
    k = _family_size()
    verdicts: list[Verdict] = []
    for dimension, buckets in _FAMILY:
        fwd_groups = _bucketed(forward_trades, dimension)
        rpl_groups = _bucketed(replay_trades, dimension)
        for bucket in _buckets_for(dimension, buckets):
            fwd = fwd_groups.get(bucket, [])
            rpl = rpl_groups.get(bucket, [])

            f_low, f_n, f_clusters, f_exp, f_thin = _bucket_bound(fwd, k)
            r_low, r_n, r_clusters, r_exp, r_thin = _bucket_bound(rpl, k)

            if _confirms(f_low, f_n, f_clusters, f_thin):
                verdicts.append(Verdict(
                    play_type=play_type, dimension=dimension, bucket=bucket,
                    tier="forward_confirmed", n=f_n, expectancy_r=f_exp,
                    ci_low=f_low, n_clusters=f_clusters, source="forward",
                ))
            elif _confirms(r_low, r_n, r_clusters, r_thin):
                verdicts.append(Verdict(
                    play_type=play_type, dimension=dimension, bucket=bucket,
                    tier="replay_screened", n=r_n, expectancy_r=r_exp,
                    ci_low=r_low, n_clusters=r_clusters, source="replay",
                ))
            else:
                # Hunch: carry the richer book for display (forward if it has any closed
                # trades, else replay), but stamp source "none" -- nothing was confirmed.
                if f_n > 0:
                    disp_low, disp_n, disp_clusters, disp_exp = f_low, f_n, f_clusters, f_exp
                elif r_n > 0:
                    disp_low, disp_n, disp_clusters, disp_exp = r_low, r_n, r_clusters, r_exp
                else:
                    disp_low, disp_n, disp_clusters, disp_exp = 0.0, 0, 0, 0.0
                verdicts.append(Verdict(
                    play_type=play_type, dimension=dimension, bucket=bucket,
                    tier="hunch", n=disp_n, expectancy_r=disp_exp,
                    ci_low=disp_low, n_clusters=disp_clusters, source="none",
                ))
    return verdicts

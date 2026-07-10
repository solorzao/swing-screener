"""Settlement: the Forward Books state machine -- one card per registered experiment.

The invariant: ARMS and VARIANTS are judged by different statistics because they are
different experiments. Arms duplicate every fill (same entry economics, different exit
management), so an arm is same-sample and is judged by ``paired_arm_delta`` -- pairing
on the fill identity removes the entry noise, and ``n_accrued`` counts PAIRS where both
legs closed (smaller than either arm's own closed count: a pair with one leg still open
has no realized delta yet). Variants are separate books (different entries), so a
variant is judged by the clustered TWO-SAMPLE bootstrap, ``clustered_two_sample_delta_low``
called at the 2.5th and 97.5th percentiles over each book's per-ticker R. Applying one
primitive to both kinds is statistically dishonest by the repo's own rules (propose.py's
D1 note); every card LABELS its upper bound ('clustered' vs 'iid') instead of hiding
which machinery produced it.

State precedence: retired > futile-awaiting-decision > settled-awaiting-decision >
accruing. A book can be simultaneously settled-by-width AND futile (a tight interval
sitting entirely below the MDE); futility wins because it is the more decision-forcing
signal -- "this experiment cannot reach its minimum detectable effect" demands an
answer, while "the interval is tight" merely permits one. Futility tests the UPPER
bound against ``mde_r`` (never "lower bound < 0"), and settlement additionally refuses
a thin (too-few-clusters) bound: a tight interval the clustered bootstrap could not
harden is not evidence. Retired experiments still produce cards -- falsified history
stays legible, with the registry's decision text shown verbatim.

Pure module: no DB session, no FastAPI. The injected ``book_loader`` is the only data
seam (the API layer binds it to ``load_closed_paper_trades(session, ...)``), so the
whole state machine runs on in-memory trade lists in tests.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from swing_screener.analytics.performance import (
    MIN_LEADERBOARD_N,
    PerformanceSummary,
    _is_closed_filled,
    clustered_two_sample_delta_low,
    cost_level_for,
    paired_arm_delta,
    summarize,
    trailing_expectancy,
)
from swing_screener.cockpit.stats import Stat, stat_from_summary
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.registry import Experiment
from swing_screener.pipeline.variants import DEFAULT_VARIANT

# Below this many accrued results the CI-shrinkage extrapolation is noise on noise:
# emit None (an honest "can't project yet"), never a fabricated n_needed.
_MIN_N_FOR_PROJECTION = 5
_ACCRUAL_WINDOW_DAYS = 30
_SPARK_POINTS = 60


class BookLoader(Protocol):
    """The data seam: returns closed paper trades filtered by the given keys.

    ``None`` for ``play_type``/``arm`` means "no filter on this key". The API layer
    binds this to the DB loader; tests bind it to an in-memory list.
    """

    def __call__(
        self, *, play_type: str | None, arm: str | None, variant: str
    ) -> list[PaperTrade]: ...


@dataclass(frozen=True, kw_only=True)
class SettlementCard:
    name: str
    kind: str                    # 'arm' | 'variant'
    play_type: str
    state: str  # 'accruing' | 'settled-awaiting-decision' | 'futile-awaiting-decision' | 'retired'
    n_accrued: int
    n_needed: int | None
    eta: str | None              # ISO date, from trailing-30d accrual rate
    stopping_rule: str
    registered_sha: str
    registered_at: str
    mde_r: float
    book: Stat
    control: Stat
    delta: Stat
    upper_bound_type: str        # 'clustered' (variants) | 'iid' (arms) -- label, don't hide
    spark: list[tuple[str, float]]   # (ISO exit_date, trailing expectancy), last ~60 pts
    decision: str | None


def _facet_filter(trades: list[PaperTrade], facet: str) -> list[PaperTrade]:
    """The gold facet is entries a human could actually have taken: ``would_surface``
    must be truthy. ``None`` (legacy/replay rows) and ``False`` are both excluded
    BEFORE any math -- an unstampable row is never graded as gold."""
    if facet == "gold":
        return [t for t in trades if t.would_surface]
    return trades


def _closed_by_ticker(trades: list[PaperTrade]) -> dict[str, list[float]]:
    """Realized R per ticker over closed-filled trades -- the two-sample bootstrap's
    clusters, grouped exactly the way ``propose.py`` does."""
    d: dict[str, list[float]] = {}
    for t in trades:
        if _is_closed_filled(t) and t.realized_r is not None:
            d.setdefault(t.ticker, []).append(t.realized_r)
    return d


def _scope(play_type: str) -> str | None:
    """Registry scope -> loader filter: 'all' means the whole book (no filter)."""
    return None if play_type == "all" else play_type


def _state(
    exp: Experiment, *, n_accrued: int, halfwidth: float, upper: float, thin: bool
) -> str:
    """The uniform settlement rule (scope decision 3), precedence retired > futile >
    settled > accruing -- see the module docstring for why futile beats settled."""
    if exp.status == "retired":
        return "retired"
    if n_accrued >= MIN_LEADERBOARD_N and upper < exp.mde_r:
        return "futile-awaiting-decision"
    if (
        n_accrued >= MIN_LEADERBOARD_N
        and halfwidth <= exp.target_ci_halfwidth_r
        and not thin
    ):
        return "settled-awaiting-decision"
    return "accruing"


def _n_needed(n_accrued: int, halfwidth: float, target: float) -> int | None:
    """Closes needed for the CI half-width to shrink to ``target``, by the shrinkage
    law halfwidth ~ 1/sqrt(n). Unprojectable (n too small, interval already collapsed)
    is ``None`` -- never 0, which would read as "done"."""
    if n_accrued < _MIN_N_FOR_PROJECTION or halfwidth <= 0:
        return None
    return math.ceil(n_accrued * (halfwidth / target) ** 2)


def _eta(
    book: list[PaperTrade], *, n_accrued: int, n_needed: int | None, now: datetime
) -> str | None:
    """Projected settlement date from the trailing-30-calendar-day close rate of the
    book. No projectable n_needed or a zero rate -> ``None`` (accrual has stalled;
    a made-up date would be worse than none)."""
    if n_needed is None:
        return None
    cutoff = now.date() - timedelta(days=_ACCRUAL_WINDOW_DAYS)
    closes = sum(
        1 for t in book
        if _is_closed_filled(t) and t.exit_date is not None and t.exit_date >= cutoff
    )
    rate = closes / _ACCRUAL_WINDOW_DAYS
    if rate <= 0:
        return None
    remaining = max(0, n_needed - n_accrued)
    return (now.date() + timedelta(days=math.ceil(remaining / rate))).isoformat()


def _spark(book: list[PaperTrade]) -> list[tuple[str, float]]:
    """Trailing-expectancy sparkline over the book's closes, last ~60 points.

    Drift only, never certification: the first ~window points of
    ``trailing_expectancy`` are sub-window means (the mean of what exists so far), so
    early points carry no emphasis -- gates keep reading the hardened CI bounds."""
    return [(d.isoformat(), v) for d, v in trailing_expectancy(book)][-_SPARK_POINTS:]


def _variant_numbers(
    book: list[PaperTrade], control: list[PaperTrade],
    book_s: PerformanceSummary, ctrl_s: PerformanceSummary,
) -> tuple[float, float, float, int, int, bool]:
    """(value, ci_low, ci_high, n_accrued, n_clusters, thin) for a VARIANT: the
    clustered two-sample bootstrap, called twice for both bounds.

    ``n_clusters`` is min(book, control) and ``thin`` is either side's flag: a
    two-sample bound is only as trustworthy as its weaker side. An empty side makes the
    bootstrap return -inf ("cannot certify"); that must never reach the wire, so the
    interval collapses to the point estimate -- ``n`` is what flags it as untrustworthy
    (mirroring ``summarize``'s < 2-closes behavior)."""
    value = book_s.expectancy_r - ctrl_s.expectancy_r
    a, b = _closed_by_ticker(book), _closed_by_ticker(control)
    low = clustered_two_sample_delta_low(a, b, lower_pct=2.5)
    high = clustered_two_sample_delta_low(a, b, lower_pct=97.5)
    if not math.isfinite(low) or not math.isfinite(high):
        low = high = value
    thin = book_s.thin_clusters or ctrl_s.thin_clusters
    return value, low, high, book_s.n_closed, min(book_s.n_clusters, ctrl_s.n_clusters), thin


def _card(
    exp: Experiment, *, book_loader: BookLoader, now: datetime, facet: str
) -> SettlementCard:
    if exp.kind == "variant":
        scope = _scope(exp.play_type)
        book = _facet_filter(
            book_loader(play_type=scope, arm=BASELINE, variant=exp.name), facet
        )
        control = _facet_filter(
            book_loader(play_type=scope, arm=BASELINE, variant=exp.control), facet
        )
        book_s, ctrl_s = summarize(book), summarize(control)
        value, low, high, n_accrued, n_clusters, thin = _variant_numbers(
            book, control, book_s, ctrl_s
        )
        upper_bound_type = "clustered"
    elif exp.kind == "arm":
        trades = _facet_filter(
            book_loader(
                play_type=_scope(exp.play_type), arm=None, variant=DEFAULT_VARIANT
            ),
            facet,
        )
        book = [t for t in trades if t.arm == exp.name]
        control = [t for t in trades if t.arm == exp.control]
        book_s, ctrl_s = summarize(book), summarize(control)
        pad = paired_arm_delta(trades, arm=exp.name, baseline=exp.control)
        value, low, high = pad.mean_delta, pad.delta_ci_low, pad.delta_ci_high
        # n_accrued counts pairs where BOTH legs closed -- smaller than either arm's
        # own closed count (the card's sub-line explains why).
        n_accrued, n_clusters, thin = pad.n_pairs, pad.n_clusters, pad.thin_clusters
        upper_bound_type = "iid"
    else:  # pragma: no cover -- the registry lockstep test keeps kinds well-formed
        raise ValueError(f"unknown experiment kind {exp.kind!r} for {exp.name!r}")

    halfwidth = (high - low) / 2
    n_needed = _n_needed(n_accrued, halfwidth, exp.target_ci_halfwidth_r)
    delta = Stat(
        value=value, n=n_accrued, n_clusters=n_clusters, ci_low=low, ci_high=high,
        cost_level=cost_level_for(book), corpus_id=None, facet=facet, unit="R",
        thin_clusters=thin,
    )
    return SettlementCard(
        name=exp.name,
        kind=exp.kind,
        play_type=exp.play_type,
        state=_state(exp, n_accrued=n_accrued, halfwidth=halfwidth, upper=high, thin=thin),
        n_accrued=n_accrued,
        n_needed=n_needed,
        eta=_eta(book, n_accrued=n_accrued, n_needed=n_needed, now=now),
        stopping_rule=exp.stopping_rule,
        registered_sha=exp.registered_sha,
        registered_at=exp.registered_at,
        mde_r=exp.mde_r,
        book=stat_from_summary(
            book_s, cost_level=cost_level_for(book), corpus_id=None, facet=facet
        ),
        control=stat_from_summary(
            ctrl_s, cost_level=cost_level_for(control), corpus_id=None, facet=facet
        ),
        delta=delta,
        upper_bound_type=upper_bound_type,
        spark=_spark(book),
        decision=exp.decision,
    )


def build_cards(
    experiments: Sequence[Experiment],
    *,
    book_loader: BookLoader,
    now: datetime,
    facet: str = "research",
) -> list[SettlementCard]:
    """One ``SettlementCard`` per registered experiment, in registry order.

    ``now`` must be a UTC-aware datetime (the eta projection anchors on its date);
    ``facet`` scopes every loaded book ('gold' filters to would_surface-truthy rows
    before any math). Empty books still emit a card -- accruing at n=0 with finite
    bounds -- so a just-registered experiment is visible from day zero."""
    return [
        _card(exp, book_loader=book_loader, now=now, facet=facet) for exp in experiments
    ]

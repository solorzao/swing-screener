"""The PURE deterministic core of the per-pick insight engine -- no LLM, no I/O.

The insight engine grades each candidate's conviction from a deterministic BASELINE:
where the pick sits in the strategy's playbook (the code-owned ``Verdict`` rows the
reflection grader emits). The LLM later NUDGES this baseline (Task 3, +-1 along the
ordered ``_CONVICTIONS`` scale) -- but the floor and the ceiling are set here, in code,
so the model can never invent conviction the evidence doesn't support.

This module also owns the R-based sizing (1R scaled by conviction) and the flat
``OrderIntent`` type the renderer consumes. Everything here is a pure function:
deterministic, no network, no DB, safe to unit-test in isolation. The score banding is
keyed off the SAME published labels the grader uses (``_score_labels(_SCORE_EDGES)``) --
never re-derived here -- so a calibration-table change can't silently desync the two.
"""

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from swing_screener.analytics.performance import _score_labels
from swing_screener.db.models import AnalystCall
from swing_screener.pipeline.reflect import Verdict, _SCORE_EDGES

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from swing_screener.notify.analysis import ConvictionResult, SignalFacts

# Ordered conviction scale; the index is used for the Task-3 +-1 clamp (the LLM may
# nudge one step along this list, never jump from "avoid" to "high").
_CONVICTIONS = ("avoid", "low", "medium", "high")
# Fraction of one risk unit (1R) each conviction stakes. "avoid" stakes nothing.
_MULT = {"high": 1.0, "medium": 0.5, "low": 0.25, "avoid": 0.0}


@dataclass(frozen=True)
class OrderIntent:
    """A single fully-specified, ready-to-render trade intent for one candidate.

    Flat and frozen: it bundles the levels (entry band, stop, target), the graded
    conviction + the R-based size, and the human-facing rationale (the edge it keys on,
    the key risk, and the LLM insight). ``shares``/``risk_dollars`` are 0/0.0 when sizing
    is unconfigured -- the renderer then shows R-multiples, never a guessed dollar."""

    ticker: str
    timeframe: str
    play_type: str
    entry_floor: float
    entry_ceiling: float
    stop: float
    target: float
    conviction: str
    shares: int
    risk_dollars: float
    edge_played: str
    key_risk: str
    insight: str
    # Order spec (for the execution adapter). ``limit_price`` is COPIED from the facts'
    # entry_ceiling (the deterministic buy-at-or-below level) -- never computed here, so the
    # adapter can never lift the price. ``notional`` is derivable (shares * limit_price), so
    # it's computed where needed rather than stored.
    side: str = "long"
    limit_price: float = 0.0
    # Reserved for the Phase-4 live broker order: the manual ticket + paper adapter execute at
    # ``limit_price`` regardless, so these are carried-but-unused until a real broker reads them.
    order_type: str = "market"
    time_in_force: str = "day"


def _score_band(score: float) -> str:
    """The published score-band label a raw ``score`` falls in (lower-inclusive: a score
    exactly on an edge falls into the HIGHER band). Keyed off ``_score_labels(_SCORE_EDGES)``
    -- the SAME bands the grader buckets by -- so the pick's band names a real verdict bucket."""
    labels = _score_labels(_SCORE_EDGES)
    idx = len(_SCORE_EDGES)
    for i, edge in enumerate(_SCORE_EDGES):
        if score < edge:
            idx = i
            break
    return labels[idx]


def _edge_label(v: Verdict) -> str:
    """A one-line human-readable name for the edge a baseline keys on: the condition
    (dimension=bucket), its tier, point expectancy, and sample size."""
    return f"{v.dimension}={v.bucket} ({v.tier}, {v.expectancy_r:+.2f}R, n={v.n})"


def conviction_baseline(*, score: float, volatility_tier: str, market_trend: str | None,
                        verdicts: list[Verdict]) -> tuple[str, str]:
    """Deterministic baseline conviction + the edge it keys on, from the pick's buckets vs the
    playbook verdicts. A matching forward_confirmed verdict with a NEGATIVE lower bound is an
    avoid; else a matching forward_confirmed -> high; else replay_screened -> medium; else
    medium (neutral). market_trend None (regime unknown) simply won't match the trend dim."""
    conds = {"score": _score_band(score), "volatility_tier": volatility_tier,
             "market_trend": market_trend}
    matches = [v for v in verdicts if v.bucket == conds.get(v.dimension)]
    neg = [m for m in matches if m.tier == "forward_confirmed" and m.ci_low < 0]
    if neg:
        return "avoid", _edge_label(neg[0])
    conf = [m for m in matches if m.tier == "forward_confirmed"]
    if conf:
        return "high", _edge_label(max(conf, key=lambda m: m.ci_low))
    screened = [m for m in matches if m.tier == "replay_screened"]
    if screened:
        return "medium", _edge_label(max(screened, key=lambda m: m.ci_low))
    return "medium", "no matching playbook edge"


def size_order(*, conviction: str, entry_ceiling: float, stop: float,
               risk_unit_dollars: float, max_shares: int | None = None) -> tuple[int, float]:
    """Conviction-scaled R-based size. risk_unit_dollars is 1R; conviction scales it. shares =
    floor(budget / per-share-risk), capped at max_shares. risk_unit_dollars<=0 (unconfigured)
    or non-positive per-share -> (0, 0.0): caller renders R-multiples, never a guessed dollar."""
    per_share = entry_ceiling - stop
    if risk_unit_dollars <= 0 or per_share <= 0:
        return 0, 0.0
    budget = risk_unit_dollars * _MULT[conviction]
    shares = int(budget // per_share)
    if max_shares is not None:
        shares = min(shares, max_shares)
    return shares, shares * per_share


def build_order_intent(
    facts: "SignalFacts", conviction_result: "ConvictionResult", *, play_type: str,
    edge_played: str, risk_unit_dollars: float, max_shares: int | None = None,
) -> OrderIntent:
    """Assemble the renderer-ready ``OrderIntent`` for one pick.

    The price levels (entry band, stop, target) are copied VERBATIM from the
    deterministic ``facts`` -- never recomputed here, so the analyst can never move
    a level. The FINAL (already +-1-clamped) conviction and the insight prose come
    from ``conviction_result``; the conviction-scaled R-based size comes from
    ``size_order`` (0/0.0 when sizing is unconfigured -> renderer shows R-multiples).
    ``key_risk`` is left empty: the single biggest risk already lives inside the
    analyst's ``insight`` prose, so we don't try to re-parse it into a short field.
    The order spec is deterministic too: ``side="long"`` and ``limit_price`` is the facts'
    ``entry_ceiling`` (the buy-at-or-below ceiling, COPIED -- never computed here).
    """
    shares, risk_dollars = size_order(
        conviction=conviction_result.conviction, entry_ceiling=facts.entry_ceiling,
        stop=facts.stop, risk_unit_dollars=risk_unit_dollars, max_shares=max_shares,
    )
    return OrderIntent(
        ticker=facts.ticker,
        timeframe=facts.timeframe,
        play_type=play_type,
        entry_floor=facts.entry_floor,
        entry_ceiling=facts.entry_ceiling,
        stop=facts.stop,
        target=facts.target,
        conviction=conviction_result.conviction,
        shares=shares,
        risk_dollars=risk_dollars,
        edge_played=edge_played,
        key_risk="",
        insight=conviction_result.insight,
        side="long",
        limit_price=facts.entry_ceiling,  # COPIED from facts -- the buy-at-or-below ceiling.
    )


def record_analyst_call(
    session: "Session", *, facts: "SignalFacts", run_date: date, created_date: date,
    baseline_conviction: str, conviction_result: "ConvictionResult", model: str,
    play_type: str,
) -> AnalystCall:
    """Persist one analyst conviction call for the learning/calibration loop.

    Records the pick keys, the deterministic BASELINE and the analyst's FINAL
    conviction, the nudge reason, and the model. The call is saved UNSCORED
    (``realized_r`` / ``scored_at`` left None); a later pass grades how the nudge
    played out. Mirrors the repo's ``save_*`` helpers (add -> commit -> refresh)."""
    call = AnalystCall(
        created_date=created_date,
        ticker=facts.ticker,
        timeframe=facts.timeframe,
        play_type=play_type,
        run_date=run_date,
        baseline_conviction=baseline_conviction,
        final_conviction=conviction_result.conviction,
        nudge_reason=conviction_result.nudge_reason,
        model=model,
    )
    session.add(call)
    session.commit()
    session.refresh(call)
    return call

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

from swing_screener.analytics.performance import _score_labels
from swing_screener.pipeline.reflect import Verdict, _SCORE_EDGES

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

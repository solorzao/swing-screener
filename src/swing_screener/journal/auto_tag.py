"""Deterministic auto-tag PROPOSALS for the Personal Trade Coach.

Rules fire off the code-owned ``TradeReviewFacts`` (never the LLM). The output is a
list of *proposals* -- they are parked in ``journal_reviews.facts_json`` and are NOT
written to ``journal_trade_tags`` until Oliver confirms one: a ``kind="mistake"`` row
with ``source in {human, analyst}`` is summed as a realized-cost confession the instant
it exists (``journal.mistakes``), so an unconfirmed proposal must never land there.

Mistake proposals map to the seeded taxonomy names (moved_stop, early_exit, ...) so a
confirmed tag joins the existing mistake-cost vocabulary. Emotion and plan-deviation
are ``kind="context"`` -- diagnostic colour, never auto-counted as a mistake.
"""

from __future__ import annotations

from dataclasses import dataclass

from swing_screener.journal.coach_grade import TradeReviewFacts


@dataclass(frozen=True)
class TagProposal:
    """A suggested tag awaiting human confirmation. ``kind`` is ``mistake`` | ``context``."""

    name: str
    kind: str
    reason: str


_EMOTION_TAGS = {"fomo", "revenge", "hesitant", "greedy"}


def propose_tags(facts: TradeReviewFacts) -> list[TagProposal]:
    """Deterministic tag proposals for one reviewed trade. Empty when the trade was
    taken and managed to plan. Order is stable (mistakes first, then context)."""
    proposals: list[TagProposal] = []

    # --- mistakes (map to the seeded taxonomy) ---
    if facts.moved_stop:
        proposals.append(TagProposal(
            name="moved_stop", kind="mistake",
            reason="the override signals the stop was moved against the plan",
        ))
    if (
        facts.outcome == "other"
        and facts.result is not None
        and facts.result > 0
    ):
        proposals.append(TagProposal(
            name="early_exit", kind="mistake",
            reason="closed manually in profit before the plan's target -- confirm if the "
                   "exit was premature",
        ))

    # --- context (never auto-counted as a mistake) ---
    if facts.emotional_state in _EMOTION_TAGS:
        proposals.append(TagProposal(
            name=facts.emotional_state, kind="context",
            reason="an emotional state was logged on this discretionary trade",
        ))
    if facts.override is not None and not facts.moved_stop:
        proposals.append(TagProposal(
            name="deviation", kind="context",
            reason=f"the logged fill deviated from the plan: {facts.override}",
        ))

    return proposals

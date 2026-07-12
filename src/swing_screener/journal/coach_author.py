"""The Personal Trade Coach's narrative author -- Opus writes PROSE only.

Copies the ``pipeline.reflect.author_edge_file`` firewall: an injectable ``client``
seam (tests pass a fake), a lazy ``anthropic`` import (cockpit/worker code that only
needs the deterministic grader must not pay the SDK import), the code-owned facts
fenced as immutable ground truth in the prompt, usage captured for the spend cap, and
degrade-to-a-deterministic-template on ANY failure (missing key, API error, empty
reply). The prose NEVER owns a number: the displayed scorecard is rendered from
``facts_json`` in the router, and this text is advisory colour beside it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_screener.config_secrets import get_secret
from swing_screener.journal.coach_grade import TradeReviewFacts, facts_dict

if TYPE_CHECKING:  # the SDK is only needed to CONSTRUCT a real client (see draft_review)
    import anthropic

    from swing_screener.notify.analysis import Usage

log = logging.getLogger(__name__)

_COACH_SYSTEM = (
    "You are a trading COACH writing a short, honest review of ONE of the trader's own "
    "real trades. You are given deterministic, ground-truth facts already computed by a "
    "rules engine (rendered below inside GROUND_TRUTH fences). HARD RULE: you MUST NOT "
    "change, invent, or re-derive any number -- treat every figure (the R or $ result, "
    "hold time, whether the stop was moved) as immutable ground truth. Do NOT restate a "
    "number the facts don't give you.\n\n"
    "Write 2-4 sentences of process-focused coaching in the second person: name the "
    "concrete behaviour (e.g. moving a stop, exiting before target, an emotional entry), "
    "connect it to this trade's facts, and give one actionable adjustment. Process over "
    "outcome. No generic advice, no preamble, no process narration, no financial advice."
)


@dataclass(frozen=True)
class DraftResult:
    """The author's output: advisory prose + the captured spend (None on the fallback)."""

    text: str
    usage: Usage | None


def _fence_facts(facts: TradeReviewFacts) -> str:
    lines = [f"{k}: {v}" for k, v in facts_dict(facts).items()]
    body = "\n".join(lines)
    return f"<<<GROUND_TRUTH\n{body}\nGROUND_TRUTH"


def template_review(facts: TradeReviewFacts) -> str:
    """Deterministic fallback prose -- complete and honest, no LLM. Used when the LLM
    is disabled, over budget, or fails, so the review always ships."""
    unit = facts.unit
    res = "still open" if facts.result is None else f"{facts.result:g}{unit}"
    bits = [f"{facts.symbol}: closed {facts.outcome} for {res}"]
    if facts.hold_days is not None:
        bits.append(f"held {facts.hold_days}d")
    if facts.moved_stop:
        bits.append("the stop was moved against the plan")
    if facts.emotional_state:
        bits.append(f"logged emotion: {facts.emotional_state}")
    return "; ".join(bits) + "."


def draft_review(
    facts: TradeReviewFacts,
    *,
    prior_weaknesses: str = "",
    client: anthropic.Anthropic | None = None,
    model: str = "claude-opus-4-8",
) -> DraftResult:
    """Draft one coaching note over ``facts``. Degrades to a deterministic template on
    ANY failure (usage None on that path). ``prior_weaknesses`` is optional feed-forward
    context (the Weaknesses Profile) the coach may reference."""
    try:
        from swing_screener.notify.analysis import _capture_usage  # noqa: PLC0415

        if client is None:
            import anthropic  # noqa: PLC0415

            client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
        context = (
            f"\n\nThe trader's standing weaknesses (for context, do not restate verbatim):\n"
            f"{prior_weaknesses}" if prior_weaknesses.strip() else ""
        )
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            system=_COACH_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Review this trade.{context}\n\n{_fence_facts(facts)}",
            }],
        )
        text: str = next(
            (getattr(b, "text", "") for b in resp.content
             if getattr(b, "type", None) == "text"),
            "",
        )
        if not text.strip():
            raise ValueError("empty model response")
        return DraftResult(text=text.strip(), usage=_capture_usage(resp, model))
    except Exception:  # noqa: BLE001 -- any failure degrades; the review never blocks on the LLM
        log.warning("coach review authoring failed; using deterministic template", exc_info=True)
        return DraftResult(text=template_review(facts), usage=None)

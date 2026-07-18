"""The System Behavior Auditor's narrative author -- an INDEPENDENT voice, walled off
from the analyst it audits. Same degrade-safe firewall as the Coach author: code owns
every number (the findings are fenced as immutable ground truth), the LLM writes only
prose, and any failure degrades to a deterministic template.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from swing_screener.config_secrets import get_secret
from swing_screener.journal.coach_author import DraftResult

if TYPE_CHECKING:
    import anthropic

log = logging.getLogger(__name__)

_AUDIT_SYSTEM = (
    "You are an INDEPENDENT system-behavior AUDITOR reviewing an automated trading "
    "system's own conduct over a period. You are NOT the system's analyst and must not "
    "defend it. You are given deterministic conduct findings already computed by a rules "
    "engine (fenced below as GROUND_TRUTH). HARD RULE: you MUST NOT change, invent, or "
    "re-derive any number -- treat every count and figure as immutable ground truth.\n\n"
    "Write 2-4 sentences of plain, sober audit commentary: state whether the machine "
    "stayed within its rules (caps, reject rate, disarms) and flag any anomaly (surfacing "
    "drought, would-surface leaks, calibration drift, orphan exits) that a human should "
    "look at. Report only; recommend no config or money change. No preamble, no narration."
)


def _fence(findings: dict) -> str:
    return f"<<<GROUND_TRUTH\n{json.dumps(findings, indent=2, sort_keys=True)}\nGROUND_TRUTH"


def template_audit(findings: dict) -> str:
    """Deterministic audit prose -- no LLM. Ships whenever the LLM is off/over/failed."""
    comp = findings.get("compliance", {})
    anom = findings.get("anomaly", {})
    breaches = len(comp.get("cap_breaches") or [])
    bits = [
        f"{breaches} cap breach(es)",
        f"reject rate {comp.get('reject_rate')}",
        f"{comp.get('n_clamps', 0)} clamp(s)",
        f"{comp.get('n_disarms', 0)} disarm(s)",
        f"{anom.get('drought_days', 0)} drought day(s)",
        f"{anom.get('orphan_exit_events', 0)} orphan exit(s)",
    ]
    return "System conduct audit: " + "; ".join(bits) + "."


def draft_audit(
    findings: dict,
    *,
    client: anthropic.Anthropic | None = None,
    model: str = "claude-opus-4-8",
) -> DraftResult:
    """Draft the audit narrative over ``findings``. Degrades to a deterministic template
    on ANY failure (usage None on that path)."""
    try:
        from swing_screener.notify.analysis import _capture_usage  # noqa: PLC0415

        if client is None:
            import anthropic  # noqa: PLC0415

            client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            system=_AUDIT_SYSTEM,
            messages=[{"role": "user",
                       "content": f"Audit this period's conduct.\n\n{_fence(findings)}"}],
        )
        text: str = next(
            (getattr(b, "text", "") for b in resp.content
             if getattr(b, "type", None) == "text"),
            "",
        )
        if not text.strip():
            raise ValueError("empty model response")
        return DraftResult(text=text.strip(), usage=_capture_usage(resp, model))
    except Exception:  # noqa: BLE001 -- any failure degrades; the audit never blocks on the LLM
        log.warning("audit authoring failed; using deterministic template", exc_info=True)
        return DraftResult(text=template_audit(findings), usage=None)

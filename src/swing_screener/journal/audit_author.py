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
        # guardrail activity is EXPECTED conduct (Task 14) -- stated as facts, in the
        # brake's own vocabulary, so a braked week reads as the machine working.
        f"{comp.get('n_guardrail_trips', 0)} guardrail trip(s)",
        f"{comp.get('n_guardrail_sweeps', 0)} guardrail sweep(s)",
        f"{comp.get('n_guardrail_clamps', 0)} guardrail clamp(s)",
        f"{anom.get('drought_days', 0)} drought day(s)",
        f"{anom.get('orphan_exit_events', 0)} orphan exit(s)",
    ]
    return "System conduct audit: " + "; ".join(bits) + "."


def breach_narrative(findings: dict) -> str:
    """Deterministic narrative for ONE breach row -- states what actually fired.
    (``template_audit`` narrates a period scorecard; rendered over a single breach's
    findings it reads "0 cap breach(es)" on an ALERT row.) No LLM: breach rows are
    urgent, deterministic artifacts."""
    breach = findings.get("cap_breach")
    if isinstance(breach, dict):
        parts: list[str] = []
        notional, cap = breach.get("notional"), breach.get("notional_cap")
        if isinstance(notional, int | float) and isinstance(cap, int | float) and notional > cap:
            parts.append(f"notional {notional:.0f} vs cap {cap:.0f}")
        day_r, loss_cap = breach.get("day_r"), breach.get("loss_cap_r")
        if (isinstance(day_r, int | float) and isinstance(loss_cap, int | float)
                and day_r <= -loss_cap):
            parts.append(f"day_r {day_r:.2f} vs loss cap -{loss_cap:.2f}")
        detail = "; ".join(parts) or "cap exceeded"
        return f"Cap breach {breach.get('account')} {breach.get('date')}: {detail}."
    guardrail = findings.get("guardrail_breach")
    if isinstance(guardrail, dict):
        # code owns the numbers: audit_run composes 'detail' (and the honest 'caveat'
        # on the two day-granularity rules); this only frames them.
        detail = str(guardrail.get("detail") or "guardrail conduct breach")
        text = (f"Guardrail conduct ({guardrail.get('rule')}) "
                f"{guardrail.get('day')}: {detail}.")
        caveat = str(guardrail.get("caveat") or "")
        return f"{text} CAVEAT: {caveat}." if caveat else text
    disarms = findings.get("disarms")
    if isinstance(disarms, list) and disarms:
        bits = [
            f"at {d.get('at')}: {d.get('reason') or 'no reason recorded'}"
            f" ({d.get('orders_cancelled') or 0} order(s) cancelled)"
            for d in disarms
        ]
        return f"Disarm {findings.get('disarm_day')}: " + "; ".join(bits) + "."
    return "Hard breach recorded; see findings."


def draft_audit(
    findings: dict,
    *,
    client: anthropic.Anthropic | None = None,
    model: str,
) -> DraftResult:
    """Draft the audit narrative over ``findings``. Degrades to a deterministic template
    on ANY failure (usage None on that path).

    ``model`` is REQUIRED, no default: ``audit_run._MODEL`` is the single authoritative
    model id, passed here AND stamped on the SystemAudit row, so the provenance stamp
    always matches the model actually called (a default here duplicated that constant --
    a stamp/call mismatch hazard flagged by the 2026-07-17 audit)."""
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

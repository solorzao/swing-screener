"""Compose concise, urgent standalone alert emails (no PDF, no digest wait).

Three alert kinds live here:

* EXIT alerts — active real trades whose :class:`ExitEvent` rows fired; the
  ``message`` already carries the ticker and context.
* GUARDRAIL-TRIP alerts (Task 10) — a breaker tripped the live brake; the
  operator must hear it the moment it happens, not in tomorrow's digest.
* LIVE-REJECTION alerts (Task 10) — working live orders the venue rejected or
  canceled (the reconcile's ``rejected_live``/``canceled`` status flips): order
  truth the operator believes is still working but is not. Distinct from an
  exit alert — a venue stop-out is a FILL + CLOSE (position truth), never a
  rejection.

LEAK POSTURE (binding, mirrors ``pipeline.guardrails``): subjects and bodies
are built ONLY from internally formatted strings — breaker names, the repo's
pre-formatted reasons (echoed verbatim, never re-derived), recorded ticket
details — never raw exception text.

Pure functions only — no I/O. Reuses :class:`EmailContent` from the digest body.
"""

import html
from collections.abc import Sequence
from datetime import date
from typing import Protocol

from swing_screener.notify.body import EmailContent

_BADGE = {"hard": "🔴", "strong": "🟠", "advisory": "🟡"}


class _Event(Protocol):
    tier: str
    reason: str
    message: str


def compose_exit_alert(events: Sequence[_Event], run_date: date) -> EmailContent:
    """Concise exit-alert email. Subject is URGENT if any hard-stop event is present."""
    urgent = any(e.tier == "hard" for e in events)
    label = "URGENT Exit Alert" if urgent else "Exit Alerts"
    subject = f"Swing Screener — {label} ({run_date})"

    lines = ["Exit alerts:"]
    for e in events:
        lines.append(f"{_BADGE.get(e.tier, '')} {e.reason}: {e.message}")
    text = "\n".join(lines)

    items = "".join(
        f"<li>{_BADGE.get(e.tier, '')} {html.escape(e.reason)}: {html.escape(e.message)}</li>"
        for e in events
    )
    html_body = f"<h2>{html.escape(subject)}</h2><ul>{items}</ul>"

    return EmailContent(subject=subject, text=text, html=html_body)


class _ExecutionRow(Protocol):
    ticker: str
    side: str
    status: str
    detail: str


def compose_guardrail_alert(
    *, trip_event_id: int, breaker: str, reason: str, run_date: date
) -> EmailContent:
    """Urgent standalone alert for a tripped guardrail breaker.

    ``breaker`` is the breaker COLUMN name and ``reason`` is
    ``guardrails_repo.breached_breaker``'s pre-formatted string — both echoed
    verbatim (never re-derived, never exception text; see the module docstring's
    leak posture). The trip event id rides the body so the email pairs with the
    cockpit's trip record.
    """
    subject = f"Swing Screener — GUARDRAIL TRIPPED: {breaker} ({run_date})"
    lines = [
        f"🔴 Guardrail trip #{trip_event_id} — {breaker}",
        f"Reason: {reason}",
        "Live dispatch is HALTED. The disarm sweep (pull resting entries, restore "
        "protective stops) runs automatically with the next broker connection if it "
        "has not already completed. Clear the trip from the cockpit to re-arm.",
    ]
    text = "\n".join(lines)
    items = "".join(f"<li>{html.escape(line)}</li>" for line in lines)
    html_body = f"<h2>{html.escape(subject)}</h2><ul>{items}</ul>"
    return EmailContent(subject=subject, text=text, html=html_body)


def compose_live_rejection_alert(
    rows: Sequence[_ExecutionRow], run_date: date
) -> EmailContent:
    """Alert for live orders the venue REJECTED or CANCELED.

    Built from :class:`ExecutionLog` rows the reconcile just flipped to
    ``rejected_live``/``canceled`` — orders the operator believes are working
    but are not (order truth). A venue stop-out is a FILL + CLOSE and rides the
    EXIT alert instead; the two kinds are disjoint by construction. ``status``
    and ``detail`` are internally recorded strings (the leak posture was upheld
    when they were written).
    """
    subject = f"Swing Screener — Live Orders Rejected/Canceled ({run_date})"
    lines = ["Live orders the venue did NOT keep working:"]
    for r in rows:
        lines.append(f"🟠 {r.ticker} {r.side} — {r.status}: {r.detail}")
    text = "\n".join(lines)
    items = "".join(
        f"<li>🟠 {html.escape(r.ticker)} {html.escape(r.side)} — "
        f"{html.escape(r.status)}: {html.escape(r.detail)}</li>"
        for r in rows
    )
    html_body = f"<h2>{html.escape(subject)}</h2><ul>{items}</ul>"
    return EmailContent(subject=subject, text=text, html=html_body)

"""Compose concise, urgent exit-alert emails for active real trades.

Unlike the daily digest, exit alerts are standalone and carry no PDF — a hard
stop shouldn't wait on PDF rendering. They are built from :class:`ExitEvent`
rows whose ``message`` already carries the ticker and context.

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

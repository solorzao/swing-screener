"""Compose the digest email body (subject, plain text, and HTML).

This is the scannable summary that fronts each digest: a ranked list of the
day's picks (ticker, trade type, one-line core reason), an exit-alerts section,
and a pointer to the attached PDF where the full analysis lives.

Pure functions only — no I/O. The composer takes already-prepared picks and
alerts and renders them into an :class:`EmailContent`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from html import escape

_BADGE = {"hard": "🔴", "strong": "🟠", "advisory": "🟡"}
_TITLE = {"daily": "Daily Top 5", "weekly": "Weekly", "monthly": "Monthly"}


@dataclass(frozen=True)
class DigestPick:
    ticker: str
    trade_type: str
    core_reason: str


@dataclass(frozen=True)
class AlertLine:
    ticker: str
    tier: str  # "hard" / "strong" / "advisory"
    reason: str
    message: str


@dataclass(frozen=True)
class EmailContent:
    subject: str
    text: str
    html: str


def compose_digest_body(
    kind: str,
    run_date: date,
    picks: Sequence[DigestPick],
    exit_alerts: Sequence[AlertLine],
    *,
    has_pdf: bool,
) -> EmailContent:
    """Render a digest into subject, plain-text, and HTML bodies.

    ``kind`` selects the title ("daily"/"weekly"/"monthly"). Picks are rendered
    as a numbered list, or a "no setups" line when empty. Exit alerts, if any,
    get their own badged section. A PDF pointer is appended when ``has_pdf``.
    """
    subject = f"Swing Screener — {_TITLE[kind]} ({run_date})"

    # --- plain text ---
    lines = [subject, ""]
    if picks:
        for i, p in enumerate(picks, start=1):
            lines.append(f"{i}. {p.ticker} [{p.trade_type}] — {p.core_reason}")
    else:
        lines.append(f"No qualifying setups for {run_date}.")

    if exit_alerts:
        lines.append("")
        lines.append("Exit alerts:")
        for a in exit_alerts:
            badge = _BADGE.get(a.tier, "")
            lines.append(f"{badge} {a.ticker} — {a.reason}: {a.message}")

    if has_pdf:
        lines.append("")
        lines.append("Full analysis attached (PDF).")

    text = "\n".join(lines)

    # --- html ---
    html_parts = [f"<h2>{escape(subject)}</h2>"]
    if picks:
        items = "".join(
            f"<li>{escape(p.ticker)} [{escape(p.trade_type)}] — "
            f"{escape(p.core_reason)}</li>"
            for p in picks
        )
        html_parts.append(f"<ol>{items}</ol>")
    else:
        html_parts.append(f"<p>No qualifying setups for {escape(str(run_date))}.</p>")

    if exit_alerts:
        html_parts.append("<h3>Exit alerts</h3>")
        items = "".join(
            f"<li>{_BADGE.get(a.tier, '')} {escape(a.ticker)} — "
            f"{escape(a.reason)}: {escape(a.message)}</li>"
            for a in exit_alerts
        )
        html_parts.append(f"<ul>{items}</ul>")

    if has_pdf:
        html_parts.append("<p>Full analysis attached (PDF).</p>")

    html = "".join(html_parts)

    return EmailContent(subject=subject, text=text, html=html)

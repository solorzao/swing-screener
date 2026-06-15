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
_KIND_TITLE = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
CONTINUATION_TITLE = "Top 5 - Continuation Plays"


@dataclass(frozen=True)
class DigestPick:
    ticker: str
    name: str
    trade_type: str
    core_reason: str
    score: float
    is_deep: bool = False  # got the Opus deep analysis (vs. the standard narration)


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


def _section_text(title: str, picks: Sequence[DigestPick], run_date: date) -> list[str]:
    """A titled, numbered pick list for the plain-text body (score included)."""
    rows: list[str] = [title, ""]
    if not picks:
        rows.append(f"No qualifying setups for {run_date}.")
        return rows
    for i, p in enumerate(picks, start=1):
        label = f"{p.ticker} - {p.name}" if p.name else p.ticker
        tag = f"{p.trade_type} · Deep Analysis" if p.is_deep else p.trade_type
        rows.append(f"{i}. {label} [{tag}] · score {p.score:.2f} — {p.core_reason}")
    return rows


def _section_html(title: str, picks: Sequence[DigestPick], run_date: date) -> str:
    """A titled, numbered pick list for the HTML body, with bold ticker + score."""
    if not picks:
        return f"<h3>{escape(title)}</h3><p>No qualifying setups for {escape(str(run_date))}.</p>"
    items = "".join(
        f"<li><b>{escape(p.ticker)}</b>"
        f"{' — ' + escape(p.name) if p.name else ''} "
        f"· [{escape(p.trade_type)}{' · <b>Deep Analysis</b>' if p.is_deep else ''}]"
        f" · score <b>{p.score:.2f}</b><br>{escape(p.core_reason)}</li>"
        for p in picks
    )
    return f"<h3>{escape(title)}</h3><ol>{items}</ol>"


def compose_digest_body(
    kind: str,
    run_date: date,
    picks: Sequence[DigestPick],
    exit_alerts: Sequence[AlertLine],
    *,
    has_pdf: bool,
) -> EmailContent:
    """Render a digest into subject, plain-text, and HTML bodies.

    ``kind`` selects the subject ("daily"/"weekly"/"monthly"). ``picks`` are the
    continuation plays, rendered under their section title (the subject is NOT
    repeated in the body). Exit alerts, if any, get their own badged section, and
    a PDF pointer is appended when ``has_pdf``.
    """
    subject = f"Swing Screener - {_KIND_TITLE[kind]} Picks ({run_date})"

    # --- plain text ---
    lines = _section_text(CONTINUATION_TITLE, picks, run_date)
    if exit_alerts:
        lines += ["", "Exit alerts:"]
        for a in exit_alerts:
            lines.append(f"{_BADGE.get(a.tier, '')} {a.ticker} — {a.reason}: {a.message}")
    if has_pdf:
        lines += ["", "Full analysis attached (PDF)."]
    text = "\n".join(lines)

    # --- html ---
    html_parts = [_section_html(CONTINUATION_TITLE, picks, run_date)]
    if exit_alerts:
        items = "".join(
            f"<li>{_BADGE.get(a.tier, '')} {escape(a.ticker)} — "
            f"{escape(a.reason)}: {escape(a.message)}</li>"
            for a in exit_alerts
        )
        html_parts.append(f"<h3>Exit alerts</h3><ul>{items}</ul>")
    if has_pdf:
        html_parts.append("<p>Full analysis attached (PDF).</p>")
    html = "".join(html_parts)

    return EmailContent(subject=subject, text=text, html=html)

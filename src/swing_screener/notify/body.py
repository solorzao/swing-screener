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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swing_screener.notify.ticker_report import TickerReport

_BADGE = {"hard": "🔴", "strong": "🟠", "advisory": "🟡"}
_KIND_TITLE = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
CONTINUATION_TITLE = "Top 5 - Continuation Plays"
REVERSAL_TITLE = "Top 5 - Reversal Plays"


@dataclass(frozen=True)
class OrderIntentLine:
    """The renderer-facing order-intent summary for a pick (insight engine, Part B).

    Conviction + the conviction-scaled size + the deterministic levels + the edge the
    baseline keyed on. ``shares``/``risk_dollars`` are 0/0.0 when sizing is unconfigured
    -- the renderer then shows R-multiples (the conviction) rather than a guessed dollar."""

    conviction: str
    shares: int
    risk_dollars: float
    edge_played: str
    entry_floor: float
    entry_ceiling: float
    stop: float
    target: float

    def text(self) -> str:
        """One scannable line: conviction, size (or R-multiples), levels, edge."""
        size = (f"{self.shares} shares (${self.risk_dollars:.0f} risk)"
                if self.shares > 0 else "R-multiples (sizing unconfigured)")
        return (
            f"Order intent: {self.conviction.upper()} conviction · {size} · "
            f"entry {self.entry_floor:g}-{self.entry_ceiling:g}, stop {self.stop:g}, "
            f"target {self.target:g} · edge: {self.edge_played}"
        )


@dataclass(frozen=True)
class OrderTicketLine:
    """The renderer-facing order ticket: what the execution adapter DID with an intent.

    Rendered only for picks whose intent was actually dispatched (execution armed); the
    "off" default never produces one, so the digest is byte-for-byte unchanged there. It
    pairs the deterministic order spec (side/limit/shares/stop/target -- all COPIED from
    the intent, never recomputed) with the adapter's ``OrderResult.status`` + detail (e.g.
    "recorded" / "filled_paper" / "skipped: <reason>")."""

    side: str
    shares: int
    ticker: str
    limit_price: float
    stop: float
    target: float
    status: str
    detail: str

    def text(self) -> str:
        """One scannable line: the order spec, then the adapter status (+ skip reason)."""
        spec = (
            f"{self.side} {self.shares} {self.ticker} @<= {self.limit_price:g}, "
            f"stop {self.stop:g}, target {self.target:g}"
        )
        # A skip carries its reason; every other status is shown bare ("recorded" etc.).
        tail = f"skipped: {self.detail}" if self.status == "skipped" else self.status
        return f"Order ticket: {spec} — {tail}"


@dataclass(frozen=True)
class DigestPick:
    ticker: str
    name: str
    trade_type: str
    core_reason: str
    score: float
    strength: str | None = None  # reversal only: "early" / "confirmed"
    is_deep: bool = False  # got the Opus deep analysis (vs. the standard narration)
    order_intent: OrderIntentLine | None = None  # insight engine: conviction + sized intent
    order_ticket: OrderTicketLine | None = None  # execution: what the adapter did (armed only)


def _tag(p: "DigestPick", *, html: bool = False) -> str:
    """The bracketed tag: trade type, then reversal strength, then a deep marker."""
    parts = [p.trade_type]
    if p.strength:
        parts.append(p.strength)
    if p.is_deep:
        parts.append("<b>Deep Analysis</b>" if html else "Deep Analysis")
    return " · ".join(parts)


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
        rows.append(f"{i}. {label} [{_tag(p)}] · score {p.score:.2f} — {p.core_reason}")
        if p.order_intent is not None:
            rows.append(f"   {p.order_intent.text()}")
        if p.order_ticket is not None:
            rows.append(f"   {p.order_ticket.text()}")
    return rows


def _section_html(title: str, picks: Sequence[DigestPick], run_date: date) -> str:
    """A titled, numbered pick list for the HTML body, with bold ticker + score."""
    if not picks:
        return f"<h3>{escape(title)}</h3><p>No qualifying setups for {escape(str(run_date))}.</p>"
    items = "".join(
        f"<li><b>{escape(p.ticker)}</b>"
        f"{' — ' + escape(p.name) if p.name else ''} "
        f"· [{_tag(p, html=True)}] · score <b>{p.score:.2f}</b><br>{escape(p.core_reason)}"
        f"{_intent_html(p)}{_ticket_html(p)}</li>"
        for p in picks
    )
    return f"<h3>{escape(title)}</h3><ol>{items}</ol>"


def _intent_html(p: "DigestPick") -> str:
    """The order-intent line for the HTML body, or empty when the pick has none."""
    if p.order_intent is None:
        return ""
    return f"<br><i>{escape(p.order_intent.text())}</i>"


def _ticket_html(p: "DigestPick") -> str:
    """The order-ticket line for the HTML body, or empty when the pick wasn't dispatched."""
    if p.order_ticket is None:
        return ""
    return f"<br><i>{escape(p.order_ticket.text())}</i>"


def compose_digest_body(
    kind: str,
    run_date: date,
    picks: Sequence[DigestPick],
    exit_alerts: Sequence[AlertLine],
    *,
    has_pdf: bool,
    reversal_picks: Sequence[DigestPick] | None = None,
    proposals_text: Sequence[str] | None = None,
    proposals_html: str | None = None,
) -> EmailContent:
    """Render a digest into subject, plain-text, and HTML bodies.

    ``kind`` selects the subject ("daily"/"weekly"/"monthly"). ``picks`` are the
    continuation plays. ``reversal_picks`` (when not None) adds a second
    "Reversal Plays" section -- pass an empty list to show it as "no setups", or
    None to omit the section entirely (weekly/monthly). Exit alerts get their own
    badged section, and a PDF pointer is appended when ``has_pdf``.

    ``proposals_text``/``proposals_html`` (the manual-mode "Proposed orders — place on
    Robinhood" shopping list, already rendered in :mod:`notify.proposals`) are appended
    when present -- the approval posture only; every other mode passes them as None, so
    the body is byte-for-byte unchanged there.
    """
    subject = f"Swing Screener - {_KIND_TITLE[kind]} Picks ({run_date})"

    # --- plain text ---
    lines = _section_text(CONTINUATION_TITLE, picks, run_date)
    if reversal_picks is not None:
        lines += ["", *_section_text(REVERSAL_TITLE, reversal_picks, run_date)]
    if proposals_text:
        lines += list(proposals_text)
    if exit_alerts:
        lines += ["", "Exit alerts:"]
        for a in exit_alerts:
            lines.append(f"{_BADGE.get(a.tier, '')} {a.ticker} — {a.reason}: {a.message}")
    if has_pdf:
        lines += ["", "Full analysis attached (PDF)."]
    text = "\n".join(lines)

    # --- html ---
    html_parts = [_section_html(CONTINUATION_TITLE, picks, run_date)]
    if reversal_picks is not None:
        html_parts.append(_section_html(REVERSAL_TITLE, reversal_picks, run_date))
    if proposals_html:
        html_parts.append(proposals_html)
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


def compose_ticker_report_body(report: "TickerReport") -> EmailContent:
    """Render an on-demand single-ticker report into subject, plain-text, and HTML.

    The email is a scannable cover for the attached multi-timeframe PDF: the overall
    summary, then one line per timeframe (HA trend + RSI), then a pointer to the PDF.
    """
    subject = (
        f"Swing Screener — Deep Read: {report.ticker} "
        f"({report.run_at.strftime('%b %d')})"
    )

    # --- plain text ---
    lines: list[str] = [report.summary, ""]
    for r in report.reads:
        lines.append(f"{r.timeframe}: {r.ha_trend}, RSI {r.rsi:.0f}")
    lines += ["", "Full report attached (PDF)."]
    text = "\n".join(lines)

    # --- html ---
    items = "".join(
        f"<li>{escape(r.timeframe)}: {escape(r.ha_trend)}, RSI {r.rsi:.0f}</li>"
        for r in report.reads
    )
    label = f"{report.ticker} - {report.name}" if report.name else report.ticker
    html = (
        f"<h2>{escape(label)}</h2>"
        f"<p>{escape(report.summary)}</p>"
        f"<ul>{items}</ul>"
        "<p>Full report attached (PDF).</p>"
    )

    return EmailContent(subject=subject, text=text, html=html)

"""Build the detailed digest PDF — one PDF per digest, a section per pick.

The email body is a scannable summary; this PDF carries the depth. Each pick
gets a section: a heading (ticker / trade type / score), the annotated chart
image when present, a levels table (entry zone, stop, target, R:R, tags, MTF),
and the Claude rationale. A missing or nonexistent chart degrades gracefully —
the section renders without the image rather than crashing.
"""

import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from swing_screener.storage.blob import blob_enabled, download_bytes

if TYPE_CHECKING:
    from swing_screener.notify.proposals import ProposedOrder
    from swing_screener.notify.ticker_report import TickerReport

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PdfPick:
    ticker: str
    name: str
    trade_type: str
    score: float
    chart_path: str | None
    entry_floor: float
    entry_ceiling: float
    stop: float
    target: float
    risk_reward: float
    quality_tier: str
    volatility_tier: str
    oversold: bool
    mtf_aligned: bool
    atr_pct: float  # ATR as a fraction of price (e.g. 0.023 == 2.3%), not dollars
    rationale: str
    is_deep: bool = False  # got the Opus deep analysis -> labelled + structured
    strength: str | None = None  # reversal only: "early" / "confirmed"
    # Insight engine (Part B): the graded order intent. None for non-insight picks; when
    # present, an "Order intent" block (conviction + sized shares/risk + edge) is rendered.
    conviction: str | None = None
    shares: int = 0
    risk_dollars: float = 0.0
    edge_played: str = ""
    # Execution (Task 6): the order ticket the adapter produced, when execution is armed.
    # None for the "off" default -> the "Order ticket" block isn't rendered (today's output).
    ticket_status: str | None = None
    ticket_detail: str = ""
    ticket_side: str = ""
    ticket_limit_price: float = 0.0


_CHART_WIDTH = 6.5 * inch


def _chart_image(source: bytes | str) -> Image:
    """A chart Image at a fixed width with its NATIVE aspect ratio preserved, so the
    candles never stretch. ``source`` is PNG bytes (blob) or a file path (local)."""
    reader = ImageReader(io.BytesIO(source) if isinstance(source, bytes) else source)
    iw, ih = reader.getSize()
    height = _CHART_WIDTH * ih / iw if iw else 3.2 * inch
    img = io.BytesIO(source) if isinstance(source, bytes) else source
    return Image(img, width=_CHART_WIDTH, height=height)


def _rationale_flowables(text: str, styles: dict) -> list:
    """Render a rationale into one Paragraph per line, bolding ``Label:`` prefixes.

    The deep analyst emits labelled lines (Read/Technicals/Fundamentals/Sentiment/
    Risk/Sources); the standard narrator emits a single blob. Either way we split
    on newlines, escape the text (model output is free-form -- raw ``<``/``&`` would
    break reportlab), and bold a short leading ``Label:`` so the structure reads.
    """
    flows: list = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        head, sep, rest = line.partition(":")
        if sep and len(head) <= 24 and not head.startswith(("-", "http")):
            flows.append(Paragraph(
                f"<b>{_xml_escape(head)}:</b> {_xml_escape(rest.strip())}", styles["BodyText"]))
        else:
            flows.append(Paragraph(_xml_escape(line), styles["BodyText"]))
    return flows


def _order_intent_flowables(p: PdfPick, styles: dict) -> list:
    """The "Order intent" block for an insight-engine pick (conviction + size + edge), or
    empty when the pick has no conviction (non-insight picks render exactly as before).

    Size shows the conviction-scaled shares + dollar risk when sizing is configured, else
    "R-multiples (sizing unconfigured)" -- never a guessed dollar."""
    if p.conviction is None:
        return []
    size = (f"{p.shares} shares (${p.risk_dollars:.0f} risk)"
            if p.shares > 0 else "R-multiples (sizing unconfigured)")
    body = (
        f"<b>Order intent:</b> {_xml_escape(p.conviction.upper())} conviction &nbsp; "
        f"{_xml_escape(size)}"
    )
    if p.edge_played:
        body += f"<br/><b>Edge:</b> {_xml_escape(p.edge_played)}"
    return [Paragraph(body, styles["BodyText"]), Spacer(1, 0.1 * inch)]


def _order_ticket_flowables(p: PdfPick, styles: dict) -> list:
    """The "Order ticket" block for a pick whose intent the adapter dispatched, or empty
    when execution is off (``ticket_status is None``) -- so the default PDF is unchanged.

    Shows the deterministic order spec (side/limit/shares/stop/target, COPIED from the
    intent) and the adapter's status (a skip carries its reason)."""
    if p.ticket_status is None:
        return []
    spec = (
        f"{p.ticket_side} {p.shares} {p.ticker} @&le; {p.ticket_limit_price:g}, "
        f"stop {p.stop:g}, target {p.target:g}"
    )
    tail = f"skipped: {p.ticket_detail}" if p.ticket_status == "skipped" else p.ticket_status
    body = f"<b>Order ticket:</b> {_xml_escape(spec)} &nbsp; {_xml_escape(tail)}"
    return [Paragraph(body, styles["BodyText"]), Spacer(1, 0.1 * inch)]


def build_story(picks: Sequence[PdfPick]) -> list:
    """Build the reportlab flowables (the "story") for ``picks``.

    Factored out of :func:`build_digest_pdf` so the section content (headings,
    levels table) is testable without parsing the rendered PDF. An empty
    sequence yields a single placeholder paragraph.
    """
    styles = getSampleStyleSheet()
    story: list = []

    if not picks:
        story.append(Paragraph("No picks.", styles["BodyText"]))
        return story

    for i, p in enumerate(picks):
        name_part = f" - {p.name}" if p.name else ""
        kind_part = f"{p.trade_type} · {p.strength}" if p.strength else p.trade_type
        story.append(
            Paragraph(
                f"{p.ticker}{name_part} &nbsp; [{kind_part}] "
                f"&nbsp; score {p.score:.2f}",
                styles["Title"],
            )
        )
        story.append(Spacer(1, 0.1 * inch))
        if blob_enabled():
            # chart_path is a blob KEY, not a filesystem path -- stat'ing it would
            # always miss, so fetch by key instead. A missing/aged-out blob (any
            # exception) degrades to the same chartless section as the local path.
            if p.chart_path:
                try:
                    story.append(_chart_image(download_bytes(p.chart_path)))
                    story.append(Spacer(1, 0.1 * inch))
                except Exception:  # noqa: BLE001 -- degrade chartless, but never silently
                    log.warning("blob chart fetch failed for %s (key %s); "
                                "rendering chartless", p.ticker, p.chart_path)
        elif p.chart_path and Path(p.chart_path).exists():
            story.append(_chart_image(p.chart_path))
            story.append(Spacer(1, 0.1 * inch))
        levels = [
            ["Company", p.name],
            ["Entry zone", f"{p.entry_floor:.2f} - {p.entry_ceiling:.2f}"],
            ["Stop", f"{p.stop:.2f}"],
            ["Target", f"{p.target:.2f}"],
            ["Reward : Risk", f"{p.risk_reward:.2f} : 1"],
            ["ATR (% of price)", f"{p.atr_pct:.1%}"],
            ["Quality / Volatility", f"{p.quality_tier} / {p.volatility_tier}"],
            ["MTF aligned / Oversold", f"{p.mtf_aligned} / {p.oversold}"],
        ]
        table = Table(levels, colWidths=[2.2 * inch, 3.5 * inch])
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 0.15 * inch))
        for flow in _order_intent_flowables(p, styles):
            story.append(flow)
        for flow in _order_ticket_flowables(p, styles):
            story.append(flow)
        if p.is_deep:  # flag the richer Opus output so it's distinguishable at a glance
            story.append(Paragraph(
                '<font color="#1a5fb4"><b>DEEP ANALYSIS</b></font>', styles["BodyText"]))
            story.append(Spacer(1, 0.05 * inch))
        for flow in _rationale_flowables(p.rationale, styles):
            story.append(flow)
        if i < len(picks) - 1:
            story.append(PageBreak())

    return story


def build_proposals_story(proposals: "Sequence[ProposedOrder]") -> list:
    """Flowables for the consolidated "Proposed orders — place on Robinhood" section.

    Mirrors the email body's shopping list: a header + one human-placeable instruction per
    proposal. Pure rendering of the already-built proposals -- empty in -> empty out (so the
    section appears only when there's something to place)."""
    from swing_screener.notify.proposals import PROPOSED_HEADER

    if not proposals:
        return []
    styles = getSampleStyleSheet()
    story: list = [
        PageBreak(),
        Paragraph(
            f'<font color="#1a5fb4"><b>{_xml_escape(PROPOSED_HEADER)}</b></font>',
            styles["Title"]),
        Spacer(1, 0.15 * inch),
    ]
    for p in proposals:
        story.append(Paragraph(_xml_escape(p.instruction()), styles["BodyText"]))
        story.append(Spacer(1, 0.05 * inch))
    return story


def build_digest_pdf(picks: Sequence[PdfPick], out_path: Path, *,
                     reversal_picks: Sequence[PdfPick] | None = None,
                     header: str | None = None,
                     proposals: "Sequence[ProposedOrder] | None" = None) -> Path:
    """Render ``picks`` (continuation) into a multi-section PDF and return it.

    ``header`` (e.g. "Swing Screener - Daily Picks (Jun 15, 2026)") is drawn ONCE
    at the top -- so the run/as-of date lives in one place rather than on every
    chart. One section per pick, separated by page breaks. ``reversal_picks``, when
    non-empty, are appended after a "REVERSAL PLAYS" divider. A pick whose
    ``chart_path`` is ``None``/missing renders without its image; an empty
    ``picks`` sequence still produces a valid (placeholder) PDF.

    ``proposals`` (the manual-mode "Proposed orders" shopping list), when non-empty,
    appends the consolidated placeable-instructions section after the picks -- the
    approval posture only; None/empty leaves the PDF unchanged.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    story: list = []
    if header:
        story.append(Paragraph(_xml_escape(header), styles["Heading2"]))
        story.append(Spacer(1, 0.2 * inch))
    story.extend(build_story(picks))
    if reversal_picks:
        story.append(PageBreak())
        story.append(Paragraph(
            '<font color="#b4561a"><b>REVERSAL PLAYS</b></font>', styles["Title"]))
        story.append(Spacer(1, 0.15 * inch))
        story.extend(build_story(reversal_picks))
    if proposals:
        story.extend(build_proposals_story(proposals))
    doc = SimpleDocTemplate(str(out_path), pagesize=letter)
    doc.build(story)
    return out_path


def build_ticker_story(report: "TickerReport") -> list:
    """Flowables for a single-ticker multi-timeframe report -- testable without rendering.

    Title + run-time caption (with a DEEP ANALYSIS marker when ``report.is_deep``),
    the overall stance, then one block per :class:`TimeframeRead` (heading, chart when
    resolvable, levels table or a "no setup" note), and finally the full narrative.
    Chart resolution mirrors :func:`build_story`: blob KEY vs. local filesystem path.
    """
    styles = getSampleStyleSheet()
    story: list = []

    name_part = f" - {report.name}" if report.name else ""
    story.append(Paragraph(f"{report.ticker}{name_part}", styles["Title"]))
    caption = report.run_at.strftime("%Y-%m-%d %H:%M")
    if report.is_deep:
        caption += ' &nbsp; <font color="#1a5fb4"><b>DEEP ANALYSIS</b></font>'
    story.append(Paragraph(caption, styles["BodyText"]))
    story.append(Spacer(1, 0.1 * inch))

    if report.summary:
        story.append(Paragraph(f"<b>{_xml_escape(report.summary)}</b>", styles["BodyText"]))
        story.append(Spacer(1, 0.15 * inch))

    for r in report.reads:
        heading = (
            f"{r.timeframe} — {r.ha_trend}, "
            f"EMA {'aligned' if r.ema_aligned else 'crossed'}, "
            f"RSI {r.rsi:.0f}, ATR {r.atr_pct:.1%}"
        )
        story.append(Paragraph(_xml_escape(heading), styles["Heading3"]))
        if blob_enabled():
            # chart_path is a blob KEY here -- fetch by key; a missing/aged-out blob
            # (any exception) degrades to a chartless block, same as the local path.
            if r.chart_path:
                try:
                    story.append(_chart_image(download_bytes(r.chart_path)))
                    story.append(Spacer(1, 0.1 * inch))
                except Exception:  # noqa: BLE001 -- degrade chartless, but never silently
                    log.warning("blob chart fetch failed for %s %s (key %s); "
                                "rendering chartless", report.ticker, r.timeframe,
                                r.chart_path)
        elif r.chart_path and Path(r.chart_path).exists():
            story.append(_chart_image(r.chart_path))
            story.append(Spacer(1, 0.1 * inch))
        if r.setup is not None:
            levels = [
                ["Entry", f"{r.setup.entry_floor:.2f} - {r.setup.entry_ceiling:.2f}"],
                ["Stop", f"{r.setup.stop:.2f}"],
                ["Target", f"{r.setup.target:.2f}"],
            ]
            table = Table(levels, colWidths=[2.2 * inch, 3.5 * inch])
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
                        ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ]
                )
            )
            story.append(table)
        else:
            story.append(Paragraph("No setup firing on this timeframe.", styles["BodyText"]))
        story.append(Spacer(1, 0.15 * inch))

    for flow in _rationale_flowables(report.analysis_text, styles):
        story.append(flow)

    return story


def build_ticker_report_pdf(report: "TickerReport", out_path: Path) -> Path:
    """Render the report to a letter-size PDF and return ``out_path``."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out_path), pagesize=letter)
    doc.build(build_ticker_story(report))
    return out_path

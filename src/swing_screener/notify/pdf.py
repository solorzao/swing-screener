"""Build the detailed digest PDF — one PDF per digest, a section per pick.

The email body is a scannable summary; this PDF carries the depth. Each pick
gets a section: a heading (ticker / trade type / score), the annotated chart
image when present, a levels table (entry zone, stop, target, R:R, tags, MTF),
and the Claude rationale. A missing or nonexistent chart degrades gracefully —
the section renders without the image rather than crashing.
"""

import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
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


@dataclass(frozen=True)
class PdfPick:
    ticker: str
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
    rationale: str


def build_digest_pdf(picks: Sequence[PdfPick], out_path: Path) -> Path:
    """Render ``picks`` into a multi-section PDF at ``out_path`` and return it.

    One section per pick, separated by page breaks. A pick whose ``chart_path``
    is ``None`` or does not exist on disk is rendered without its image. An
    empty ``picks`` sequence still produces a valid (placeholder) PDF.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(out_path), pagesize=letter)
    story: list = []

    if not picks:
        story.append(Paragraph("No picks.", styles["BodyText"]))
        doc.build(story)
        return out_path

    for i, p in enumerate(picks):
        story.append(
            Paragraph(
                f"{p.ticker} &nbsp; [{p.trade_type}] &nbsp; score {p.score:.2f}",
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
                    data = download_bytes(p.chart_path)
                    story.append(
                        Image(io.BytesIO(data), width=6.5 * inch, height=3.2 * inch)
                    )
                    story.append(Spacer(1, 0.1 * inch))
                except Exception:
                    pass
        elif p.chart_path and Path(p.chart_path).exists():
            story.append(Image(p.chart_path, width=6.5 * inch, height=3.2 * inch))
            story.append(Spacer(1, 0.1 * inch))
        levels = [
            ["Entry zone", f"{p.entry_floor:.2f} - {p.entry_ceiling:.2f}"],
            ["Stop", f"{p.stop:.2f}"],
            ["Target", f"{p.target:.2f}"],
            ["R:R", f"{p.risk_reward:.2f}"],
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
        story.append(Paragraph(p.rationale, styles["BodyText"]))
        if i < len(picks) - 1:
            story.append(PageBreak())

    doc.build(story)
    return out_path

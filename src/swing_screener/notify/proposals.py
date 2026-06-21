"""The consolidated "Proposed orders — place on Robinhood" surface (Phase-5 Task 2).

The approval posture (``execution_mode="manual"``) records an order ticket per pick but
moves no money and contacts no broker. This module turns those recorded tickets into the
human's actionable "shopping list": one copy-placeable instruction per proposal, rendered
into the digest body (text + HTML) and the PDF, plus a small structured JSON artifact the
human (or a future review→place export) acts on by hand.

Pure rendering only -- every level (limit, stop, target) and the share count are COPIED
from the already-built :class:`OrderIntent`; nothing is recomputed here. A proposal is
included only when its ticket was actually RECORDED (``status == "recorded"``); a
limit-blocked / skipped ticket is dropped from the placeable list (it isn't actionable).
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from html import escape
from pathlib import Path

from swing_screener.notify.body import OrderTicketLine
from swing_screener.pipeline.insight import OrderIntent

log = logging.getLogger(__name__)

PROPOSED_HEADER = "Proposed orders — place on Robinhood"


@dataclass(frozen=True)
class ProposedOrder:
    """One placeable proposal: the deterministic order spec + the graded conviction.

    Every field is COPIED from the built :class:`OrderIntent` (the single source of truth);
    nothing is recomputed. This is the shape written to the JSON artifact verbatim."""

    ticker: str
    side: str
    limit_price: float
    shares: int
    stop: float
    target: float
    conviction: str
    play_type: str

    def instruction(self) -> str:
        """The exact, human-placeable Robinhood instruction for this proposal."""
        return (
            f"Buy {self.ticker} — limit <= ${self.limit_price:.2f}, {self.shares} shares "
            f"({self.conviction.upper()} conviction); then set stop ${self.stop:.2f}, "
            f"target ${self.target:.2f}"
        )


def build_proposals(
    intents: Sequence[OrderIntent], tickets: dict[tuple[str, str], OrderTicketLine]
) -> list[ProposedOrder]:
    """Pair each built intent with its recorded ticket -> the placeable proposals.

    Only intents whose ticket was RECORDED (``status == "recorded"``) become proposals -- a
    skipped (limit-blocked) ticket isn't actionable, so it's dropped from the shopping list.
    Levels + conviction are taken from the ``OrderIntent`` (never recomputed); the ticket is
    consulted only for its dispatch ``status``."""
    proposals: list[ProposedOrder] = []
    for intent in intents:
        ticket = tickets.get((intent.ticker, intent.play_type))
        if ticket is None or ticket.status != "recorded":
            continue  # not dispatched, or skipped/limit-blocked -> not placeable
        proposals.append(ProposedOrder(
            ticker=intent.ticker, side=intent.side, limit_price=intent.limit_price,
            shares=intent.shares, stop=intent.stop, target=intent.target,
            conviction=intent.conviction, play_type=intent.play_type))
    return proposals


def proposals_text(proposals: Sequence[ProposedOrder]) -> list[str]:
    """The plain-text "Proposed orders" block (header + one instruction per proposal)."""
    if not proposals:
        return []
    rows = ["", PROPOSED_HEADER, ""]
    rows.extend(f"- {p.instruction()}" for p in proposals)
    return rows


def proposals_html(proposals: Sequence[ProposedOrder]) -> str:
    """The HTML "Proposed orders" block, or empty when there are none."""
    if not proposals:
        return ""
    items = "".join(f"<li>{escape(p.instruction())}</li>" for p in proposals)
    return f"<h3>{escape(PROPOSED_HEADER)}</h3><ul>{items}</ul>"


def proposals_artifact_path(pdf_dir: Path, run_date: date) -> Path:
    """The JSON artifact path alongside the PDF: ``proposed_orders_<YYYYMMDD>.json``."""
    return Path(pdf_dir) / f"proposed_orders_{run_date:%Y%m%d}.json"


def write_proposals_artifact(
    proposals: Sequence[ProposedOrder], pdf_dir: Path, run_date: date
) -> Path | None:
    """Write the proposals as a JSON list next to the PDF; best-effort, never blocks.

    Mirrors the PDF seam: any failure (e.g. an unwritable dir) is swallowed + logged and
    returns ``None`` rather than blocking the digest. Returns the written path on success.
    Callers gate this on ``manual`` mode + a non-empty ``proposals`` list."""
    if not proposals:
        return None
    path = proposals_artifact_path(pdf_dir, run_date)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([asdict(p) for p in proposals], indent=2), encoding="utf-8")
        return path
    except Exception:  # the artifact must never block the digest
        log.warning("proposed-orders artifact write failed for %s", run_date, exc_info=True)
        return None

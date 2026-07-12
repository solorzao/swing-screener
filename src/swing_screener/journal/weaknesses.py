"""The Weaknesses Profile builder -- the living distillation of the trader's recurring
mistakes across their reviewed manual trades.

Pure derivation over the code-owned ``journal_reviews.facts_json`` (never the LLM): a
handful of deterministic rules count how often each weakness shows up, and a signal
that recurs at least ``min_occurrences`` times becomes a profile item with its evidence
as ``(book, trade_id)`` pairs (Coach producer id spaces overlap, so a bare id is
ambiguous). Below ``thin_floor`` reviews the profile is stamped ``thin_data`` -- it is
honest colour, not a verdict, until the corpus is real (design SS Risks). Append-only:
the latest ``weaknesses_profiles`` row is current; ``generated_at`` is its staleness stamp.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import JournalReview, WeaknessesProfile

# (label, predicate over the parsed facts_json dict). Deterministic; no LLM.
_WEAKNESS_RULES: list[tuple[str, Callable[[dict], bool]]] = [
    ("moves stops under heat", lambda f: f.get("moved_stop") is True),
    ("exits winners early",
     lambda f: f.get("outcome") == "other" and (f.get("result") or 0) > 0),
    ("trades on emotion",
     lambda f: f.get("emotional_state") in {"fomo", "revenge", "greedy", "hesitant"}),
]

_MIN_OCCURRENCES = 2
_THIN_DATA_FLOOR = 5


def build_profile(
    session: Session,
    *,
    now: datetime,
    min_occurrences: int = _MIN_OCCURRENCES,
    thin_floor: int = _THIN_DATA_FLOOR,
) -> WeaknessesProfile:
    """Distill the trade-close reviews into a fresh Weaknesses Profile row and return
    it. Idempotent only in the sense of append -- each call writes a new current row
    (history preserved). ``now`` is injected for determinism (workflow-safe)."""
    reviews = list(session.scalars(
        select(JournalReview)
        .where(JournalReview.kind == "trade_close")
        .order_by(JournalReview.id)
    ))

    hits: dict[str, list[dict[str, object]]] = {label: [] for label, _ in _WEAKNESS_RULES}
    review_dates = []
    for r in reviews:
        try:
            facts = json.loads(r.facts_json or "{}")
        except (ValueError, TypeError):
            facts = {}
        for label, pred in _WEAKNESS_RULES:
            if pred(facts):
                hits[label].append({"book": r.book, "trade_id": r.trade_id})
        if r.generated_at is not None:
            review_dates.append(r.generated_at)

    items = [
        {"weakness": label, "count": len(evidence), "evidence": evidence}
        for label, evidence in hits.items()
        if len(evidence) >= min_occurrences
    ]

    payload = {
        "items": items,
        "n_reviews": len(reviews),
        "thin_data": len(reviews) < thin_floor,
    }
    profile = WeaknessesProfile(
        scope="personal",
        items_json=json.dumps(payload),
        covered_from=min(review_dates).date() if review_dates else None,
        covered_to=now.date(),
        generated_at=now,
    )
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return profile

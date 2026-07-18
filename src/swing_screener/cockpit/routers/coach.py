"""The Personal Trade Coach cockpit surface (reads Oliver's real manual trades only).

Reads: the per-trade reviews for a personal book and the current Weaknesses Profile.
Writes (header-guarded, nonce-bumped): edit a review's prose, and CONFIRM a parked
auto-tag proposal -- the confirm gate. A proposal lives in the review's ``facts_json``
until confirmed; only on confirm is a ``source="analyst"`` tag written to the overlay
(a mistake tag is a live confession the instant it exists).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.cockpit.common import ActionNonce, _require_cockpit, _utc_iso
from swing_screener.db.models import JournalReview, WeaknessesProfile
from swing_screener.journal.repo import add_tag, tag_trade

log = logging.getLogger(__name__)

_PERSONAL_BOOKS = ("manual_equity", "robinhood")


class ReviewEdit(BaseModel):
    """PATCH the human's edited prose onto a review (bounded to the Text column use)."""

    human_edit: str = Field(max_length=10_000)


class ConfirmTag(BaseModel):
    """Confirm one parked proposal -> write the overlay tag. ``kind`` is mistake|context."""

    name: str = Field(max_length=64)
    kind: str = Field(max_length=16)


def _review_dict(r: JournalReview) -> dict[str, object]:
    return {
        "id": r.id,
        "kind": r.kind,
        "book": r.book,
        "trade_id": r.trade_id,
        "generated_at": _utc_iso(r.generated_at),
        "model": r.model,
        "est_cost_usd": r.est_cost_usd,
        "narrative": r.narrative,
        "human_edit": r.human_edit,
        "facts": json.loads(r.facts_json or "{}"),
    }


def build_coach_router(
    *,
    _session: Callable[[], Iterator[Session]],
    action_nonce: ActionNonce,
) -> APIRouter:
    """Coach endpoints, closed over the session dep + the post-action wake nonce."""
    router = APIRouter()

    @router.get("/api/coach/reviews")
    def reviews(
        book: str = "manual_equity", session: Session = Depends(_session)
    ) -> list[dict[str, object]]:
        """Every review for a PERSONAL book, newest first. Machine books are never
        served here -- the Coach voice is only ever applied to Oliver's own trades."""
        if book not in _PERSONAL_BOOKS:
            raise HTTPException(status_code=422, detail="not a personal book")
        rows = session.scalars(
            select(JournalReview).where(JournalReview.book == book)
            .order_by(JournalReview.id.desc())
        )
        out: list[dict[str, object]] = []
        for r in rows:
            try:
                out.append(_review_dict(r))
            except ValueError:  # corrupt facts_json: per-row degrade, never a 500
                log.warning("skipping review %s: corrupt facts_json", r.id)
        return out

    @router.get("/api/coach/weaknesses")
    def weaknesses(session: Session = Depends(_session)) -> dict[str, object]:
        """The current (latest) Weaknesses Profile, or an empty shell if none yet."""
        row = session.scalars(
            select(WeaknessesProfile).order_by(WeaknessesProfile.id.desc()).limit(1)
        ).first()
        if row is None:
            return {"items": [], "thin_data": True, "n_reviews": 0, "generated_at": None}
        raw = json.loads(row.items_json or "{}")
        # The column default is "[]" (a bare items LIST) while the builder writes
        # the full dict -- normalize a list into the documented shape (no reviews
        # counted yet, honestly thin) instead of TypeError-ing into a 500.
        payload: dict[str, object] = (
            raw if isinstance(raw, dict)
            else {"items": raw, "thin_data": True, "n_reviews": 0}
        )
        payload["generated_at"] = _utc_iso(row.generated_at)
        return payload

    @router.post(
        "/api/coach/reviews/{review_id}/edit",
        dependencies=[Depends(_require_cockpit)],
    )
    def edit_review(
        review_id: int, body: ReviewEdit, session: Session = Depends(_session)
    ) -> dict[str, object]:
        row = session.get(JournalReview, review_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no review {review_id}")
        row.human_edit = body.human_edit
        session.commit()
        action_nonce.bump()
        return _review_dict(row)

    @router.post(
        "/api/coach/reviews/{review_id}/confirm-tag",
        dependencies=[Depends(_require_cockpit)],
    )
    def confirm_tag(
        review_id: int, body: ConfirmTag, session: Session = Depends(_session)
    ) -> dict[str, object]:
        """Apply a parked auto-tag proposal to the overlay as ``source="analyst"``.
        The proposal must exist in this review's facts, and the review must be a
        per-trade review (a rollup has no single trade to tag)."""
        row = session.get(JournalReview, review_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no review {review_id}")
        if row.trade_id is None:
            raise HTTPException(status_code=422, detail="review has no trade to tag")
        facts = json.loads(row.facts_json or "{}")
        proposals = facts.get("tag_proposals", [])
        if not any(p.get("name") == body.name and p.get("kind") == body.kind
                   for p in proposals):
            raise HTTPException(status_code=422, detail="no such parked proposal")
        tag = add_tag(session, kind=body.kind, name=body.name)
        tag_trade(session, trade_id=row.trade_id, book=row.book, tag_id=tag.id,
                  source="analyst")
        action_nonce.bump()
        return {"applied": {"name": body.name, "kind": body.kind}, "trade_id": row.trade_id}

    return router

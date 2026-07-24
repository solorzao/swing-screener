"""The Personal Trade Coach worker: drain the on-close draft queue + weekly rollup.

``coach_run`` is an Azure Container Apps Job (default-off, spend-capped). It does BOTH:
1. drains ``coach_draft_requests`` -- for each, backfills the review's narrative
   (Opus prose when the coach is enabled and under budget, else the deterministic
   template) and its usage columns;
2. refreshes the Weaknesses Profile (weekly rollup).

Copies ``notify.ondemand``'s drain shape: ``requeue_stale`` FIRST, then claim, then a
per-request ``process_one`` that NEVER raises (one bad draft never aborts the batch;
only the exception CLASS is persisted, never ``str(exc)`` which can leak hosts/keys).
The spend accumulator mirrors ``notify.run`` -- once ``coach_max_usd`` is reached the
remaining reviews stay facts-only (template narrative), never blocking on the LLM.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.config_secrets import get_secret
from swing_screener.db import repo
from swing_screener.db.models import CoachDraftRequest, JournalReview, WeaknessesProfile
from swing_screener.db.session import get_engine
from swing_screener.journal.coach_author import DraftResult, draft_review, template_review
from swing_screener.journal.coach_grade import TradeReviewFacts
from swing_screener.journal.weaknesses import build_profile
from swing_screener.pipeline.run import _migrate_with_retry, _resolve_db_url
from swing_screener.settings import Settings, load_settings

if TYPE_CHECKING:
    import anthropic

log = logging.getLogger(__name__)

_STALE_AFTER = timedelta(minutes=30)
# The ONE authoritative model id for the coach (2026-07-17 audit: a duplicated constant
# here + a separate default in coach_author could stamp a model the call never used).
# It is passed EXPLICITLY to draft_review AND stamped on the JournalReview row, so
# provenance always matches the model actually called. Coach prose is bounded 600-token
# template-grade output (2-4 sentences over code-owned facts), so claude-haiku-4-5 at
# $1/$5 per MTok replaces claude-opus-4-8 at $5/$25 -- ~25x cheaper (2026-07-17 cost
# plan). notify.analysis._MODEL_PRICES carries a claude-haiku-4-5 row, so the coach
# spend cap keeps metering. No env override by design; the escape hatch is this line.
_MODEL = "claude-haiku-4-5"


def _facts_from_review(review: JournalReview) -> TradeReviewFacts:
    """Rebuild the grader facts from the code-owned ``facts_json`` (ignores the parked
    tag proposals and any other extra keys)."""
    data = json.loads(review.facts_json or "{}")
    names = {f.name for f in fields(TradeReviewFacts)}
    return TradeReviewFacts(**{k: v for k, v in data.items() if k in names})


def _latest_weaknesses_text(session: Session) -> str:
    """A short feed-forward context string from the current Weaknesses Profile ("" if
    none). The coach may reference the trader's standing weaknesses."""
    row = session.scalars(
        select(WeaknessesProfile).order_by(WeaknessesProfile.id.desc()).limit(1)
    ).first()
    if row is None:
        return ""
    try:
        items = json.loads(row.items_json).get("items", [])
    except (ValueError, TypeError):
        return ""
    return "; ".join(str(i.get("weakness", "")) for i in items)


def process_one(
    session: Session,
    request: CoachDraftRequest,
    *,
    settings: Settings,
    now: datetime,
    spend: list[float],
    client: anthropic.Anthropic | None,
) -> None:
    """Draft ONE review's narrative. NEVER raises -- fails the row on any error so the
    batch continues. Backfills ``narrative`` (+ usage cols when the LLM ran)."""
    try:
        review = session.get(JournalReview, request.review_id)
        if review is None:
            repo.fail_coach_draft_request(
                session, request.id, error="review missing", finished_at=now)
            return
        facts = _facts_from_review(review)
        over_budget = (
            settings.coach_max_usd is not None and spend[0] >= settings.coach_max_usd
        )
        if settings.coach_enabled and not over_budget:
            draft = draft_review(
                facts, prior_weaknesses=_latest_weaknesses_text(session), client=client,
                model=_MODEL)
            if draft.usage is not None:
                spend[0] += draft.usage.est_cost_usd
        else:
            draft = DraftResult(text=template_review(facts), usage=None)

        review.narrative = draft.text
        if draft.usage is not None:
            review.model = _MODEL
            review.input_tokens = draft.usage.input_tokens
            review.output_tokens = draft.usage.output_tokens
            review.est_cost_usd = draft.usage.est_cost_usd
        session.commit()
        repo.complete_coach_draft_request(session, request.id, finished_at=now)
    except Exception as exc:
        log.exception("coach draft request %s failed", request.id)
        session.rollback()
        repo.fail_coach_draft_request(
            session, request.id, error=f"error ({type(exc).__name__})", finished_at=now)


def process_pending(
    session: Session,
    *,
    settings: Settings,
    now: datetime,
    limit: int = 10,
    client: anthropic.Anthropic | None = None,
) -> int:
    """Requeue crashed-worker rows, claim a batch, and drain each. Returns the number
    claimed. The spend accumulator is shared across the batch."""
    repo.requeue_stale_coach_drafts(session, cutoff=now - _STALE_AFTER)
    claimed = repo.claim_queued_coach_drafts(session, now=now, limit=limit)
    spend = [0.0]
    for request in claimed:
        process_one(session, request, settings=settings, now=now, spend=spend, client=client)
    return len(claimed)


def _build_client() -> anthropic.Anthropic | None:
    """Construct one real Anthropic client for the batch, or None if the SDK/key is
    absent (draft_review then degrades to the template per call)."""
    try:
        import anthropic

        return anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
    except Exception:  # noqa: BLE001 -- no key/SDK -> template path, never a crash
        log.warning("coach client unavailable; drafts will use the deterministic template")
        return None


def main() -> None:
    """`python -m swing_screener.journal.coach_run` -- drain the draft queue + refresh
    the Weaknesses Profile. DB-writing ACA Job (default-off coach still drains to
    template narratives)."""
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Drain Coach on-close drafts + rollup.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--skip-rollup", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    db_url = _resolve_db_url(args.db)
    if db_url.startswith("mssql"):  # Azure SQL: Alembic owns the schema, upgrade first
        _migrate_with_retry(db_url)
    engine = get_engine(db_url)
    now = datetime.now(UTC)
    with Session(engine) as session:
        client = _build_client() if settings.coach_enabled else None
        drained = process_pending(
            session, settings=settings, now=now, limit=args.limit, client=client)
        if not args.skip_rollup:
            build_profile(session, now=now)
    log.info("coach_run: drained=%d", drained)


if __name__ == "__main__":
    main()

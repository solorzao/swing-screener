"""CoachDraftRequest queue repo: enqueue, race-safe claim, stale requeue, complete/fail."""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from swing_screener.db.repo import (
    claim_queued_coach_drafts,
    complete_coach_draft_request,
    create_coach_draft_request,
    fail_coach_draft_request,
    requeue_stale_coach_drafts,
)
from swing_screener.db.session import get_engine

_T0 = datetime(2026, 7, 12, 14, 0, 0)


def test_enqueue_then_claim_flips_to_running():
    with Session(get_engine("sqlite:///:memory:")) as s:
        req = create_coach_draft_request(s, review_id=7, requested_at=_T0)
        assert req.status == "queued"
        claimed = claim_queued_coach_drafts(s, now=_T0 + timedelta(seconds=1))
        assert [c.review_id for c in claimed] == [7]
        assert claimed[0].status == "running"
        # a second claim finds nothing left queued
        assert claim_queued_coach_drafts(s, now=_T0 + timedelta(seconds=2)) == []


def test_complete_and_fail_set_terminal_status():
    with Session(get_engine("sqlite:///:memory:")) as s:
        a = create_coach_draft_request(s, review_id=1, requested_at=_T0)
        b = create_coach_draft_request(s, review_id=2, requested_at=_T0)
        claim_queued_coach_drafts(s, now=_T0 + timedelta(seconds=1))
        complete_coach_draft_request(s, a.id, finished_at=_T0 + timedelta(minutes=1))
        fail_coach_draft_request(s, b.id, error="error (RuntimeError)",
                                 finished_at=_T0 + timedelta(minutes=1))
        s.refresh(a)
        s.refresh(b)
        assert a.status == "done" and b.status == "failed"
        assert b.error == "error (RuntimeError)"


def test_requeue_stale_running_recovers_a_crashed_worker():
    with Session(get_engine("sqlite:///:memory:")) as s:
        create_coach_draft_request(s, review_id=3, requested_at=_T0)
        claim_queued_coach_drafts(s, now=_T0)   # stuck 'running' at _T0, never finished
        # cutoff later than the claim time -> the stale row is requeued
        n = requeue_stale_coach_drafts(s, cutoff=_T0 + timedelta(minutes=31))
        assert n == 1
        again = claim_queued_coach_drafts(s, now=_T0 + timedelta(hours=1))
        assert [c.review_id for c in again] == [3]

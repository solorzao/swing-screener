"""CoachDraftRequest queue repo: enqueue, race-safe claim, stale requeue, complete/fail."""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from swing_screener.db.models import CoachDraftRequest
from swing_screener.db.repo import (
    claim_queued_coach_drafts,
    complete_coach_draft_request,
    create_coach_draft_request,
    fail_coach_draft_request,
    requeue_stale_coach_drafts,
)
from swing_screener.db.session import get_engine

_T0 = datetime(2026, 7, 12, 14, 0, 0)  # noqa: DTZ001 -- naive fixture compared to sqlite round-tripped naive values


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


def test_claim_stamps_microsecond_free_token():
    """The claim must truncate its token to whole seconds BEFORE stamping.

    Callers pass full-precision ``datetime.now(UTC)`` (coach_run.py); SQL Server's
    DATETIME stores at 1/300s ticks (rounded), so a microsecond-bearing stamp never
    equals the full-precision bind parameter in the ``started_at == now`` read-back
    -- the claim returns [] on Azure SQL and drafts stall 'running' forever. Whole
    seconds are exactly representable in DATETIME, so microsecond-free STORAGE is
    the portable property that makes the equality hold on every backend. (SQLite
    round-trips microseconds exactly, so we pin the stored value instead.)
    """
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        req = create_coach_draft_request(s, review_id=9, requested_at=_T0)
        req_id = req.id
        claimed = claim_queued_coach_drafts(
            s, now=datetime(2026, 7, 17, 12, 0, 0, 123456))  # noqa: DTZ001 -- naive fixture compared to sqlite round-tripped naive values
        assert [c.review_id for c in claimed] == [9]
        assert claimed[0].started_at.microsecond == 0
    # Fresh session (not the identity-mapped object above): what actually got STORED
    # is microsecond-free, so DATETIME rounding is a no-op.
    with Session(engine) as s2:
        stored = s2.get(CoachDraftRequest, req_id)
        assert stored is not None
        assert stored.started_at == datetime(2026, 7, 17, 12, 0, 0)  # noqa: DTZ001 -- naive fixture compared to sqlite round-tripped naive value


def test_sequential_claims_keep_their_own_rows():
    """Race-safety survives truncation: each claim stamps its own whole-second token
    and only reads back rows carrying THAT token, so two workers claiming at
    different times never return each other's rows."""
    with Session(get_engine("sqlite:///:memory:")) as s:
        create_coach_draft_request(s, review_id=1, requested_at=_T0)
        create_coach_draft_request(s, review_id=2, requested_at=_T0 + timedelta(minutes=1))
        first = claim_queued_coach_drafts(s, now=_T0 + timedelta(seconds=10), limit=1)
        second = claim_queued_coach_drafts(s, now=_T0 + timedelta(seconds=11), limit=1)
        assert [c.review_id for c in first] == [1]
        assert [c.review_id for c in second] == [2]
        assert first[0].started_at == _T0 + timedelta(seconds=10)
        assert second[0].started_at == _T0 + timedelta(seconds=11)


def test_requeue_stale_running_recovers_a_crashed_worker():
    with Session(get_engine("sqlite:///:memory:")) as s:
        create_coach_draft_request(s, review_id=3, requested_at=_T0)
        claim_queued_coach_drafts(s, now=_T0)   # stuck 'running' at _T0, never finished
        # cutoff later than the claim time -> the stale row is requeued
        n = requeue_stale_coach_drafts(s, cutoff=_T0 + timedelta(minutes=31))
        assert n == 1
        again = claim_queued_coach_drafts(s, now=_T0 + timedelta(hours=1))
        assert [c.review_id for c in again] == [3]

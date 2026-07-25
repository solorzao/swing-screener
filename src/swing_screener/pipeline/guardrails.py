"""The trip protocol: evaluate breakers, and on a trip run the ORDERED response --
(1) persist the tripped state + event FIRST (the brake holds no matter what
follows), (2) the disarm sweep (entry pulls + stop protection, ``key_suffix``
from the TRIP id so cross-process re-runs collapse to the same
client_order_ids), (3) record the sweep outcome on the trip row, (4) the alert
email (isolated try/except; a mail failure never aborts a sweep). Also the
re-run owner: while ``sweep_state`` is 'pending'/'partial', every caller re-runs
the sweep -- ``ensure_stop_protection`` skips already-protected symbols and
``pull_entry_orders`` cancels only what's open, so re-runs are safe.

Ownership: ``guardrails_repo.trip()``'s rows-affected election picks exactly ONE
trip-response owner across the racing processes (digest / screen / cockpit); a
lost election means another process owns the sweep + email -- stand down
entirely. The trip EVENT is still appended by ``trip()`` itself either way (the
breach was real, whoever won).

LEAK POSTURE (binding, see ``execution._guardrail_block``): trip reasons are
internally formatted breaker strings (``guardrails_repo.breached_breaker``) --
they echo verbatim into cockpit-visible ``ExecutionLog.detail`` and
``trip_reason``. A failed sweep's recorded detail is ``broker_error_detail``'s
class-name-only wording, NEVER raw exception text (broker/httpx messages embed
venue hosts and credentials); the full traceback goes to the log.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy.orm import Session

from swing_screener.db import guardrails_repo, repo
from swing_screener.db.models import AgentGuardrailEvent, DisarmEvent
from swing_screener.pipeline.broker import BrokerClient, BrokerOrder, broker_error_detail
from swing_screener.pipeline.disarm import run_protective_sweep

log = logging.getLogger(__name__)

#: the sweep_state values that mean "the sweep has not finished -- re-run it".
_INCOMPLETE_SWEEPS = ("pending", "partial")


def evaluate_breakers(session: Session, *, run_date: date) -> tuple[str, str] | None:
    """``(breaker, reason)`` for the first BREACHED breaker, else None.

    A read-only DECISION -- but not a pure read: ``load_guardrails`` commits on
    its get-or-create seed (``breached_breaker`` is the pure half), so call this
    between transactions, never with uncommitted session work pending.
    Deliberately does NOT consult state -- state is the trip's OUTCOME, not an
    input (an already-HALTED book with a breached drawdown still needs the trip
    recorded; the repo election lets a trip overwrite 'halted' by design). The
    caller gates on state, skipping evaluation ONLY when already 'tripped' (the
    trip owner ran the response; re-tripping would just spam trip events).
    Delegates the four per-breaker checks to
    ``guardrails_repo.breached_breaker`` -- the SAME definition the submit-side
    clamp (``execution._guardrail_block``) enforces, so the loop and the adapter
    can never disagree about a breach."""
    g = guardrails_repo.load_guardrails(session)
    return guardrails_repo.breached_breaker(session, g, run_date=run_date)


def consult(
    session: Session, *, run_date: date, source: str,
    broker: BrokerClient | None,
    emailer: Callable[[int, str, str], None] | None = None,
) -> Literal["tripped", "halted"] | None:
    """The shared guardrails consult: resume -> load -> evaluate-unless-tripped ->
    respond. ONE definition for all three cycles -- the digest dispatch loop, the
    evening screen, and the hourly exit check -- so they can never drift on the
    skip-when-tripped rule or the ordered trip protocol (Task 11; the quadruplet
    used to be copy-pasted at each site).

    DAY-KEY CONVENTION (binding, see the design doc's "Day semantics"): the
    ``run_date`` handed in is the TRADING DAY OF RECORD, and outside the evening
    screen every caller resolves it as ``repo.latest_run_date(session) or
    date.today()`` -- the same key the digest stamps on ``ExecutionLog.run_date``
    and on the ``PaperTrade.exit_date`` its dispatch-time reconcile books. Only
    the evening screen mints a NEW day (it passes its own ``today``, the date it
    is writing signals for). This matters because the breakers are run_date
    scoped: an hourly consult keyed on the wall clock would count zero of the
    day's own live orders in ``max_trades_per_day`` (the breaker is then
    structurally dead there) and would book exits on a day the daily-loss
    breaker never queries.

    Ordered exactly like the dispatch loop this generalizes:

    1. ``resume_incomplete_sweep`` -- the re-run owner finishes a prior trip's
       pending/partial sweep first (state-checked internally; ``broker`` None is
       a quiet no-op).
    2. Load state; evaluation is skipped ONLY when already 'tripped' (the trip
       owner ran the response; re-tripping would just spam trip events). A
       breach during a manual HALT still records its trip: run_date-scoped
       breakers evaporate when the date advances, and the repo election
       deliberately lets a trip overwrite 'halted'.
    3. On a breach: ``respond_to_trip`` runs the full ordered protocol
       (persist-first, sweep, outcome, mail; ``broker`` None defers the sweep to
       the next cycle's resume, ``emailer`` None skips the mail step).

    Returns the BLOCKING state -- ``'tripped'`` (a fresh breach responded to
    here, or an already-tripped book) or ``'halted'`` -- or None when dispatch
    may proceed. Any response BEYOND the protocol stays with the caller: the
    dispatch loop breaks its batch and runs the manual-HALT sweep on
    ``'halted'``; the screen and the hourly job dispatch nothing, so they call
    this bare and ignore the verdict. FAIL-SAFE on the unexpected: any state
    that is neither 'ok' nor one of the two known blocking values is reported as
    ``'tripped'`` (the conservative bucket -- an unrecognized brake state must
    BLOCK, never wave dispatch through) with a loud log naming it.
    """
    resume_incomplete_sweep(session, broker=broker, source=source)
    g = guardrails_repo.load_guardrails(session)
    breach = (None if g.state == "tripped"
              else evaluate_breakers(session, run_date=run_date))
    if breach is not None:
        respond_to_trip(session, breaker=breach[0], reason=breach[1],
                        source=source, broker=broker, emailer=emailer)
        return "tripped"
    if g.state == "ok":
        return None
    if g.state == "halted":
        return "halted"
    if g.state != "tripped":
        log.error("unexpected guardrails state %r -- blocking dispatch as 'tripped' "
                  "(fail-safe)", g.state)
    return "tripped"


def respond_to_trip(
    session: Session, *, breaker: str, reason: str, source: str,
    broker: BrokerClient | None,
    emailer: Callable[[int, str, str], None] | None = None,
) -> int | None:
    """The full ordered protocol: persist, sweep, record, mail.

    ``guardrails_repo.trip()``'s election decides ownership -- None means
    another process owns the response: return None quietly, touch NOTHING (no
    venue call, no DisarmEvent, no email; the breach's trip event was still
    appended by ``trip()`` itself). ``broker`` None (the evening-screen secrets
    gap): the trip persists with ``sweep_state='pending'`` and the sweep is
    skipped -- the next cycle owns it via ``resume_incomplete_sweep``.
    ``emailer(trip_event_id, breaker, reason)`` is the Task-10 seam; None skips
    step 4, and a raising emailer is swallowed (the sweep outcome must survive a
    dead mailer). Returns the trip event id when this call won the election.

    NOTE: a trades/day trip's sweep cancels the day's own just-submitted resting
    entries too -- intended, and conservative: once the cap is hit, nothing
    unfilled may keep working (a fill after the trip would grow exposure exactly
    when the brake said stop)."""
    # (1) PERSIST-FIRST: the tripped state + trip event commit BEFORE any venue
    # call -- the brake holds even if everything after this line dies.
    trip_id = guardrails_repo.trip(session, breaker=breaker, reason=reason, source=source)
    if trip_id is None:
        log.info("guardrail trip election lost (breaker=%s): another process owns "
                 "the response", breaker)
        return None
    log.warning("GUARDRAIL TRIP %d (%s): %s -- running the disarm sweep",
                trip_id, breaker, reason)
    if broker is None:
        # trip() already stamped sweep_state='pending'; the next cycle with a
        # broker re-runs the sweep (resume_incomplete_sweep).
        log.warning("guardrail trip %d: no broker available -- sweep deferred "
                    "(sweep_state stays 'pending')", trip_id)
    else:
        # (2) the sweep + (3) its recorded outcome, keyed on THE trip we won so a
        # late finish can never stamp a newer trip's bookkeeping.
        outcome, detail = _run_sweep(session, broker, trip_id=trip_id, breaker=breaker)
        _record_outcome_guarded(
            session, trip_id=trip_id, outcome=outcome, detail=detail, source=source)
    # (4) the alert email -- isolated: a mail failure never aborts (or unwinds)
    # anything above.
    if emailer is not None:
        try:
            emailer(trip_id, breaker, reason)
        except Exception:  # noqa: BLE001 -- the mail seam must never break the protocol
            log.warning("guardrail trip %d: alert email failed", trip_id, exc_info=True)
    return trip_id


def resume_incomplete_sweep(
    session: Session, *, broker: BrokerClient | None, source: str
) -> bool:
    """If state=='tripped' and sweep_state in ('pending','partial'): re-run the
    sweep and record the outcome. Returns True if a sweep ran; ``broker`` None ->
    False (nothing to sweep with -- the trip row keeps waiting). Safe to call
    every cycle: the state read is one column select, and the sweep body is
    idempotent (``pull_entry_orders`` cancels only what's open,
    ``ensure_stop_protection`` skips already-protected symbols, and the restore
    client_order_ids are keyed by the trip id so venue-side idempotency collapses
    cross-process re-runs)."""
    g = guardrails_repo.load_guardrails(session)
    if g.state != "tripped" or g.sweep_state not in _INCOMPLETE_SWEEPS:
        return False
    if broker is None:
        return False
    if g.trip_id is None:  # defensive: a tripped row always carries its trip id
        log.error("tripped guardrails row has no trip_id -- cannot key a sweep "
                  "outcome; leaving sweep_state %r", g.sweep_state)
        return False
    # the trip EVENT (trip_id is its id) carries the breaker name the DisarmEvent
    # reason wants; a missing row (never expected) degrades to an empty breaker.
    trip_event = session.get(AgentGuardrailEvent, g.trip_id)
    breaker = trip_event.breaker if trip_event is not None else ""
    log.warning("resuming incomplete guardrail sweep for trip %d (sweep_state=%s)",
                g.trip_id, g.sweep_state)
    outcome, detail = _run_sweep(session, broker, trip_id=g.trip_id, breaker=breaker)
    _record_outcome_guarded(
        session, trip_id=g.trip_id, outcome=outcome, detail=detail, source=source)
    return True


def _record_outcome_guarded(
    session: Session, *, trip_id: int, outcome: str, detail: str, source: str
) -> None:
    """``record_sweep_outcome``, isolated: the sweep already MOVED VENUE STATE,
    so a failed outcome write (dead DB, mid-UPDATE failure) must neither unwind
    the caller nor leave the shared session poisoned (PendingRollbackError would
    kill everything downstream -- for the digest, the email itself). On failure:
    log, roll back, move on -- ``sweep_state`` simply stays 'pending'/'partial'
    and the next cycle's resume re-runs the (idempotent) sweep and re-records."""
    try:
        guardrails_repo.record_sweep_outcome(
            session, trip_id=trip_id, outcome=outcome, detail=detail, source=source)
    except Exception:  # noqa: BLE001 -- bookkeeping must never outrank the caller
        log.error("failed to record sweep outcome for trip %d (%s) -- sweep_state "
                  "stays re-runnable", trip_id, outcome, exc_info=True)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 -- a dead session must not break the protocol
            log.warning("sweep-outcome rollback also failed", exc_info=True)


def _run_sweep(
    session: Session, broker: BrokerClient, *, trip_id: int, breaker: str
) -> tuple[str, str]:
    """The trip's disarm sweep -> ``(outcome, detail)``: entry pulls + stop protection.

    The venue work is ``disarm.run_protective_sweep`` -- the SAME body the
    kill-switch and manual-HALT paths run (one definition of "the sweep") --
    with the stops restored at the ExecutionLog ticket's RECORDED level (copied,
    never computed). ``key_suffix`` is derived from the TRIP id so re-runs of
    the SAME trip's sweep -- any process, any day -- collapse to the same
    client_order_ids at the venue. Full success -> ``('complete', summary)``;
    any exception -> ``('partial', class-name-only detail)`` -- the caller
    records either, so a failed sweep is visible and re-runnable, never silent.
    After the attempt (success OR partial) a best-effort DisarmEvent is written
    (``reason='guardrail:<breaker>'``): the Auditor's breach scanner already
    turns DisarmEvents into alerts, so the sweep is on the conduct record with
    zero new Auditor code. NOTE its ``orders_cancelled`` UNDER-reports when
    ``pull_entry_orders`` dies mid-cancel (``entries`` stays empty) --
    best-effort telemetry, never the ledger; the venue is the ledger."""
    entries: list[BrokerOrder] = []
    try:
        entries, restored, unprotected = run_protective_sweep(
            broker,
            lambda sym: repo.latest_recorded_stop(session, sym),
            key_suffix=f"guardrail-{trip_id}")
        outcome = "complete"
        detail = (f"swept: {len(entries)} entry order(s) cancelled, "
                  f"{len(restored)} protective stop(s) restored")
        if unprotected:
            detail += f"; UNPROTECTED: {', '.join(unprotected)}"
            log.error("guardrail sweep for trip %d: %d position(s) left UNPROTECTED: %s",
                      trip_id, len(unprotected), ", ".join(unprotected))
    except Exception as e:  # noqa: BLE001 -- a venue boundary: record 'partial', never raise
        # Leak posture: the recorded detail reaches the cockpit (guardrail events
        # render there) -- class name only; the traceback goes to the LOG.
        log.error("guardrail sweep for trip %d failed -- sweep_state stays "
                  "re-runnable ('partial')", trip_id, exc_info=True)
        outcome = "partial"
        detail = broker_error_detail(e)
    record_disarm_event(
        session, reason=f"guardrail:{breaker}", orders_cancelled=len(entries))
    return outcome, detail


def record_disarm_event(session: Session, *, reason: str, orders_cancelled: int) -> None:
    """Persist a DisarmEvent so the System Behavior Auditor sees a venue-moving
    sweep -- EVERY one of them: the trip response (``reason='guardrail:<breaker>'``),
    the mid-dispatch kill switch (``'kill-switch'``), and the manual-HALT brake
    (``'halt'``) all journal here (the cockpit's own "more alarming, not less"
    rationale: an unexplained disarm must never be invisible to the Auditor).

    Replicates the cockpit's ``_record_disarm`` posture (cockpit/routers/safety.py):
    best-effort, rollback-FIRST (a failed sweep may arrive with a poisoned
    session -- add+commit on it would always lose the event; every caller only
    ever commits before this point, so there is nothing pending to lose), and
    NEVER raises -- the sweep moved venue state, and failing to journal that
    must not unwind the caller."""
    try:
        session.rollback()
        session.add(DisarmEvent(
            created_at=datetime.now(UTC), reason=reason[:256],
            orders_cancelled=orders_cancelled))
        session.commit()
    except Exception:  # noqa: BLE001 -- audit logging is best-effort; the sweep stands
        log.warning("failed to persist guardrail DisarmEvent", exc_info=True)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 -- a dead session must not break the protocol
            log.warning("guardrail DisarmEvent rollback also failed", exc_info=True)

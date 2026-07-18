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

from sqlalchemy.orm import Session

from swing_screener.db import guardrails_repo, repo
from swing_screener.db.models import AgentGuardrailEvent, DisarmEvent
from swing_screener.pipeline.broker import BrokerClient
from swing_screener.pipeline.disarm import ensure_stop_protection, pull_entry_orders
from swing_screener.pipeline.preflight import broker_error_detail

log = logging.getLogger(__name__)

#: the sweep_state values that mean "the sweep has not finished -- re-run it".
_INCOMPLETE_SWEEPS = ("pending", "partial")


def evaluate_breakers(session: Session, *, run_date: date) -> tuple[str, str] | None:
    """``(breaker, reason)`` for the first BREACHED breaker, else None. Pure read.

    Deliberately does NOT consult state -- state is the trip's OUTCOME, not an
    input (an already-halted book with a breached drawdown still needs the trip
    recorded); the caller gates on state (the dispatch loop skips evaluation
    when the brake is already engaged). Delegates the four per-breaker checks to
    ``guardrails_repo.breached_breaker`` -- the SAME definition the submit-side
    clamp (``execution._guardrail_block``) enforces, so the loop and the adapter
    can never disagree about a breach. NOTE: ``load_guardrails`` commits on its
    get-or-create seed, so call this between transactions, never with uncommitted
    session work pending."""
    g = guardrails_repo.load_guardrails(session)
    return guardrails_repo.breached_breaker(session, g, run_date=run_date)


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
    dead mailer). Returns the trip event id when this call won the election."""
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
        guardrails_repo.record_sweep_outcome(
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
    guardrails_repo.record_sweep_outcome(
        session, trip_id=g.trip_id, outcome=outcome, detail=detail, source=source)
    return True


def _run_sweep(
    session: Session, broker: BrokerClient, *, trip_id: int, breaker: str
) -> tuple[str, str]:
    """The disarm sweep body -> ``(outcome, detail)``: entry pulls + stop protection.

    Mirrors the dispatch loop's kill-switch block: cancel every resting
    ENTRY-side order (the sell side is the protection -- never a blanket
    cancel), then make sure every open position keeps a live protective stop,
    restored at the ExecutionLog ticket's RECORDED level (copied, never
    computed). ``key_suffix`` is derived from the TRIP id so re-runs of the SAME
    trip's sweep -- any process, any day -- collapse to the same
    client_order_ids at the venue. Full success -> ``('complete', summary)``;
    any exception -> ``('partial', class-name-only detail)`` -- the caller
    records either, so a failed sweep is visible and re-runnable, never silent.
    After the attempt (success OR partial) a best-effort DisarmEvent is written
    (``reason='guardrail:<breaker>'``): the Auditor's breach scanner already
    turns DisarmEvents into alerts, so the sweep is on the conduct record with
    zero new Auditor code."""
    entries: list = []
    try:
        entries, _sells = pull_entry_orders(broker)
        restored, unprotected = ensure_stop_protection(
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
    _record_disarm_event(
        session, reason=f"guardrail:{breaker}", orders_cancelled=len(entries))
    return outcome, detail


def _record_disarm_event(session: Session, *, reason: str, orders_cancelled: int) -> None:
    """Persist a DisarmEvent so the System Behavior Auditor sees the sweep.

    Replicates the cockpit's ``_record_disarm`` posture (cockpit/routers/safety.py):
    best-effort, rollback-FIRST (a failed sweep may arrive with a poisoned
    session -- add+commit on it would always lose the event; the trip protocol
    only ever commits before this point, so there is nothing pending to lose),
    and NEVER raises -- the sweep moved venue state, and failing to journal that
    must not unwind the outcome bookkeeping that follows."""
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

"""Compose concise, urgent standalone alert emails (no PDF, no digest wait).

Three alert kinds live here:

* EXIT alerts — active real trades whose :class:`ExitEvent` rows fired; the
  ``message`` already carries the ticker and context.
* GUARDRAIL-TRIP alerts (Task 10) — a breaker tripped the live brake; the
  operator must hear it the moment it happens, not in tomorrow's digest.
* LIVE-REJECTION alerts (Task 10) — working live orders the venue REJECTED
  (the reconcile's ``rejected_live`` status flip): order truth the operator
  believes is still working but is not. Distinct from an exit alert — a venue
  stop-out is a FILL + CLOSE (position truth), never a rejection. ``canceled``
  rows are deliberately NOT mailed (Task-11 review): that status covers benign
  end-of-day DAY-order expiry AND this system's own trip/halt/kill sweep
  cancels — emailing "re-enter manually if still wanted" for orders the
  guardrails deliberately killed an hour earlier is worse than silence. They
  still ride the digest's ticket lines and the cockpit execution log.

LEAK POSTURE (binding, mirrors ``pipeline.guardrails``): subjects and bodies
are built ONLY from internally formatted strings — breaker names, the repo's
pre-formatted reasons (echoed verbatim, never re-derived), recorded ticket
details — never raw exception text.

Compose functions are pure — no I/O. The exceptions are the shared EMITTERS at
the bottom — the guardrail-trip pair (:func:`send_guardrail_alert` +
:func:`guardrail_alert_sent` + :func:`emit_pending_guardrail_alert`, owning the
``alert_key=str(trip_event_id)`` send-then-log dedup contract) and the
live-rejection pair (:func:`send_live_rejection_alert` +
:func:`pending_rejection_ids`, owning the per-ROW
``kind='execution-cover'``/``alert_key='xlog-{id}'`` coverage contract plus the
one ``kind='execution'`` DISPLAY row per sent email) — one owner per contract,
shared by every caller: ``notify.run``'s
dispatch loop, ``pipeline.run``'s evening screen, and ``pipeline.exitcheck``'s
hourly job. They live HERE because this module stays pipeline-import-free (its
only imports are ``notify.body``, sqlalchemy, and ``db.*``; the send callable
is injected), which lets the pipeline jobs import it lazily without ever
touching ``notify.run`` (the notify.run -> pipeline cycle).
"""

import hashlib
import html
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swing_screener.db import guardrails_repo
from swing_screener.db.models import AgentGuardrailEvent, EmailLog, ExecutionLog
from swing_screener.notify.body import EmailContent

_BADGE = {"hard": "🔴", "strong": "🟠", "advisory": "🟡"}


class _Event(Protocol):
    tier: str
    reason: str
    message: str


def compose_exit_alert(events: Sequence[_Event], run_date: date) -> EmailContent:
    """Concise exit-alert email. Subject is URGENT if any hard-stop event is present."""
    urgent = any(e.tier == "hard" for e in events)
    label = "URGENT Exit Alert" if urgent else "Exit Alerts"
    subject = f"Swing Screener — {label} ({run_date})"

    lines = ["Exit alerts:"]
    for e in events:
        lines.append(f"{_BADGE.get(e.tier, '')} {e.reason}: {e.message}")
    text = "\n".join(lines)

    items = "".join(
        f"<li>{_BADGE.get(e.tier, '')} {html.escape(e.reason)}: {html.escape(e.message)}</li>"
        for e in events
    )
    html_body = f"<h2>{html.escape(subject)}</h2><ul>{items}</ul>"

    return EmailContent(subject=subject, text=text, html=html_body)


class _ExecutionRow(Protocol):
    ticker: str
    side: str
    status: str
    detail: str


def compose_guardrail_alert(
    *, trip_event_id: int, breaker: str, reason: str, run_date: date
) -> EmailContent:
    """Urgent standalone alert for a tripped guardrail breaker.

    ``breaker`` is the breaker COLUMN name and ``reason`` is
    ``guardrails_repo.breached_breaker``'s pre-formatted string — both echoed
    verbatim (never re-derived, never exception text; see the module docstring's
    leak posture). The trip event id rides the body so the email pairs with the
    cockpit's trip record.
    """
    subject = f"Swing Screener — GUARDRAIL TRIPPED: {breaker} ({run_date})"
    # The REASON leads: it carries the dollar figures/counts, so a lock-screen
    # preview (subject + first body line) already answers "why".
    # NOTE: "clear the trip from the cockpit" references the guardrails panel
    # that ships on this SAME branch (Tasks 15-16) — the CTA assumes the
    # branch-atomic deploy; do not cherry-pick this task without them.
    lines = [
        f"🔴 {reason}",
        f"Guardrail trip #{trip_event_id} — {breaker}",
        "Live dispatch is HALTED. The disarm sweep (pull resting entries, restore "
        "protective stops) runs automatically with the next broker connection if it "
        "has not already completed. Clear the trip from the cockpit to re-arm.",
    ]
    text = "\n".join(lines)
    items = "".join(f"<li>{html.escape(line)}</li>" for line in lines)
    html_body = f"<h2>{html.escape(subject)}</h2><ul>{items}</ul>"
    return EmailContent(subject=subject, text=text, html=html_body)


def compose_live_rejection_alert(
    rows: Sequence[_ExecutionRow], run_date: date
) -> EmailContent:
    """Alert for live orders the venue REJECTED.

    Built from :class:`ExecutionLog` rows the reconcile just flipped to
    ``rejected_live`` — orders the operator believes are working but are not
    (order truth). A venue stop-out is a FILL + CLOSE and rides the EXIT alert
    instead; the two kinds are disjoint by construction. ``canceled`` rows are
    out of scope by CALLER contract (see the module docstring) — the wording
    here says REJECTED and must stay true to what the emitters select.
    ``status`` and ``detail`` are internally recorded strings (the leak posture
    was upheld when they were written).
    """
    # Phone-glance subject: count + tickers, so the preview alone says what
    # died. Ticker list bounded (EmailLog.subject is String(256) and Azure SQL
    # enforces it); a digest pass flips at most a handful of same-day orders.
    tickers = [r.ticker for r in rows]
    shown = ", ".join(tickers[:6]) + (", …" if len(tickers) > 6 else "")
    n = len(rows)
    plural = "s" if n != 1 else ""
    subject = (f"Swing Screener — {n} Live Order{plural} Rejected: "
               f"{shown} ({run_date})")
    action = ("Nothing auto-resubmits a rejected order — re-enter manually "
              "if still wanted.")
    lines = ["Live orders the venue did NOT keep working:"]
    for r in rows:
        lines.append(f"🟠 {r.ticker} {r.side} — {r.status}: {r.detail}")
    lines.append(action)
    text = "\n".join(lines)
    items = "".join(
        f"<li>🟠 {html.escape(r.ticker)} {html.escape(r.side)} — "
        f"{html.escape(r.status)}: {html.escape(r.detail)}</li>"
        for r in rows
    )
    html_body = (f"<h2>{html.escape(subject)}</h2><ul>{items}</ul>"
                 f"<p>{html.escape(action)}</p>")
    return EmailContent(subject=subject, text=text, html=html_body)


# --- the shared guardrail-trip emitter (the ONE owner of the dedup contract) --


#: the trip-alert ``EmailLog`` kind, keyed ``alert_key=str(trip_event_id)``. PUBLIC
#: because READERS grade against it: the System Behavior Auditor's unmailed-trip rule
#: asks whether a trip EPISODE has one of these rows (it restates the string as a
#: literal rather than take a journal -> notify import, and pins the two in its tests).
#: This module is the WRITER and therefore the owner of the value.
TRIP_ALERT_KIND = "guardrail"


def guardrail_alert_sent(session: Session, alert_key: str) -> bool:
    """True if a guardrail alert for this trip event was EVER logged (any run_date).

    Deliberately NOT date-filtered (unlike ``notify.run._exit_already_sent``):
    the trip event id is globally unique, and the retry owner may run on a
    LATER date than the trip (an evening trip mailed by the next morning's
    digest) — a date filter would double-send exactly there. The
    ``(kind, run_date, alert_key)`` unique constraint still backs the
    same-date concurrent race.
    """
    stmt = select(EmailLog).where(
        EmailLog.kind == TRIP_ALERT_KIND, EmailLog.alert_key == alert_key
    )
    return session.scalars(stmt).first() is not None


def send_guardrail_alert(session: Session, *, run_date: date, recipient: str,
                         send: Callable[..., None], trip_event_id: int,
                         breaker: str, reason: str) -> bool:
    """Send + log ONE guardrail-trip alert, deduped on ``str(trip_event_id)``.

    The single owner of the trip-alert dedup contract: EVERY trip-alert path —
    in-protocol emitters (the IMMEDIACY half) and at-least-once retry emitters
    alike, in whichever job — routes here rather than composing and logging its
    own, so one alert_key covers them all and whichever fires first wins while
    the rest no-op. SEND-then-LOG, the ``_emit_pending_exit_alert`` ordering and
    rationale: a trip alert is urgent, so we prioritize never LOSING it over
    strictly preventing a rare duplicate. A failed send leaves NO EmailLog row
    — and because trip elections happen ONCE, the in-protocol path never
    retries; the digest-side emitter is the retry owner. Returns True iff an
    email was sent.
    """
    key = str(trip_event_id)
    if guardrail_alert_sent(session, key):
        return False
    email = compose_guardrail_alert(trip_event_id=trip_event_id, breaker=breaker,
                                    reason=reason, run_date=run_date)
    send(to=recipient, subject=email.subject, text=email.text, html=email.html,
         attachments=[])  # SEND FIRST (see docstring)
    session.add(EmailLog(sent_at=datetime.now(UTC), kind=TRIP_ALERT_KIND,
                         subject=email.subject, run_date=run_date, alert_key=key))
    try:
        session.commit()
    except IntegrityError:  # lost the concurrent-replica race; the row already exists
        session.rollback()
    return True


def emit_pending_guardrail_alert(session: Session, run_date: date, recipient: str,
                                 send: Callable[..., None]) -> bool:
    """Send the alert for a tripped book whose trip email never landed — the
    AT-LEAST-ONCE half (the digest and the hourly exit job are the retry
    owners, exactly like exit alerts). Covers the evening screen's
    broker/transport-less trip and any in-protocol send that failed: if
    state=='tripped' and no ``EmailLog(kind='guardrail',
    alert_key=str(trip_id))`` exists, compose + send + log on the SAME key the
    in-protocol emitter uses, so the dedup collapses the paths. A trip the
    operator CLEARS from the cockpit before any retry cycle runs is
    deliberately never mailed — clearing implies awareness (Task 14's auditor
    treats trip-without-EmailLog as a breach and must exempt cleared trips for
    the same reason). Returns True iff an email was sent.
    """
    g = guardrails_repo.load_guardrails(session)
    if g.state != "tripped" or g.trip_id is None:
        return False
    if guardrail_alert_sent(session, str(g.trip_id)):
        return False
    # the trip EVENT carries the breaker name; the row's trip_reason is the
    # same pre-formatted string trip() stamped (fall back to the event's copy).
    trip_event = session.get(AgentGuardrailEvent, g.trip_id)
    breaker = trip_event.breaker if trip_event is not None else ""
    reason = g.trip_reason or (trip_event.reason if trip_event is not None else "")
    return send_guardrail_alert(session, run_date=run_date, recipient=recipient,
                                send=send, trip_event_id=g.trip_id,
                                breaker=breaker, reason=reason)


# --- the shared live-rejection emitter (the ONE owner of the per-row contract) --

#: the ExecutionLog statuses an alert email covers. REJECTED ONLY (Task-11
#: review): ``canceled`` also marks benign EOD DAY-order expiry and this
#: system's OWN trip/halt/kill sweep cancels, so mailing it tells the operator
#: to re-enter orders the guardrails deliberately killed. Canceled rows stay
#: visible in the digest ticket lines and the cockpit execution log.
REJECTED_STATUSES = ("rejected_live",)

#: how far back the rejection queries look. The reconcile only flips RECENT
#: ``submitted_live`` rows (DAY orders die the same session), so an unbounded
#: scan would grow forever with the table while never finding older flips.
_REJECTION_LOOKBACK_DAYS = 7

#: the per-ROW coverage kind: one row per alerted ExecutionLog id, read ONLY by
#: the coverage join. Split off ``kind='execution'`` (Task-11 review) so the
#: cockpit's email list and the activity feed show ONE entry per email sent
#: instead of N identical rows; heartbeats key on their own kinds either way.
#: PUBLIC because the cockpit's reference router filters this kind out of both
#: email surfaces (it restates the literal rather than import notify for one
#: string; tests/cockpit/test_reference.py pins the two against each other).
REJECTION_COVER_KIND = "execution-cover"

#: the DISPLAY kind: exactly one row per email actually sent (subject, run_date,
#: a set-hash alert_key) — what ``GET /api/emails`` and the ticker render.
_DISPLAY_KIND = "execution"


def _rejection_key(log_id: int) -> str:
    """The per-ROW dedup key for a live-rejection alert: ``xlog-{ExecutionLog.id}``.

    Per ROW, not per set (Task 11): the old sha1-of-the-flipped-id-SET key made
    coverage undecidable across processes — the digest alerts {5,6}, the hourly
    retry then finds {5,6,7} un-diffable, computes a DIFFERENT set hash, and
    re-alerts 5 and 6. With one EmailLog row per alerted ExecutionLog id,
    coverage is a per-row join and a later pass alerts exactly the uncovered
    rows. NOTE (one-time deploy seam): rows alerted under the legacy sha1 keys
    have no per-row coverage, so within the 7-day lookback the hourly job may
    re-alert them ONCE after this ships — accepted (live execution is not yet
    armed in prod).
    """
    return f"xlog-{log_id}"


def _display_key(ids: Sequence[int]) -> str:
    """Deterministic key over the SET of alerted ids (sha1, 40 chars) for the
    ONE display row — ``notify.run._exit_alert_key``'s idiom, numeric-sorted so
    the key is order-independent and distinguishes {1,2} from {1,2,3}. Dedup for
    THIS kind is incidental (the coverage rows already decide what gets sent);
    the key exists so the ``(kind, run_date, alert_key)`` unique constraint can
    never collide two different emails on the same day."""
    joined = ",".join(str(i) for i in sorted(ids))
    return hashlib.sha1(joined.encode()).hexdigest()


def recent_rejection_ids(session: Session, *, run_date: date) -> set[int]:
    """The ids of RECENT rejected ExecutionLog rows (the lookback window).

    The digest's before/after snapshot pair around a ``reconcile_live`` pass
    diffs two of these to get THAT pass's flips (the reconcile returns a count,
    not rows); the hourly retry instead joins the whole recent set against the
    per-row EmailLog coverage (:func:`pending_rejection_ids`).
    """
    stmt = select(ExecutionLog.id).where(
        ExecutionLog.status.in_(REJECTED_STATUSES),
        ExecutionLog.created_date >= run_date - timedelta(days=_REJECTION_LOOKBACK_DAYS),
    )
    return set(session.scalars(stmt))


def _covered_rejection_ids(session: Session, ids: set[int]) -> set[int]:
    """The subset of ``ids`` already covered by a per-row coverage EmailLog row.

    Deliberately NOT date-filtered (the ``guardrail_alert_sent`` posture): the
    retry owner may run on a later date than the alert that covered a row, and
    a date filter would double-send exactly there.
    """
    if not ids:
        return set()
    stmt = select(EmailLog.alert_key).where(
        EmailLog.kind == REJECTION_COVER_KIND,
        EmailLog.alert_key.in_([_rejection_key(i) for i in ids]),
    )
    return {int(key.removeprefix("xlog-")) for key in session.scalars(stmt)}


def pending_rejection_ids(session: Session, *, run_date: date) -> set[int]:
    """Every recent rejected ExecutionLog id with NO alert coverage.

    The hourly retry owner's query (Task 11): a digest whose rejection send
    FAILED leaves no coverage rows, and its next cycle's before-snapshot
    already contains the flipped ids — the diff never re-produces them. This
    query-based pass re-finds them for as long as they sit uncovered inside
    the lookback window.
    """
    ids = recent_rejection_ids(session, run_date=run_date)
    return ids - _covered_rejection_ids(session, ids)


def send_live_rejection_alert(session: Session, *, run_date: date, recipient: str,
                              send: Callable[..., None],
                              candidate_ids: set[int]) -> bool:
    """ONE email naming every uncovered REJECTED row in ``candidate_ids``.

    The single owner of the live-rejection dedup contract, shared by the
    digest's dispatch-time diff and the hourly query-based retry. Two filters
    decide what gets mailed: the status (``REJECTED_STATUSES`` — canceled rows
    are never alerted, enforced HERE so no caller can widen the scope by
    handing over a fatter id set) and per-ROW coverage (see
    :func:`_rejection_key`) — already-covered ids are dropped, so a partial
    overlap (digest alerted {5,6}; the hourly finds {5,6,7}) alerts ONLY the
    new row.

    SEND-then-LOG, the exit-alert ordering and rationale — a failed send leaves
    NO rows at all, so the next hourly pass retries the whole batch. The writes
    after the send are: ONE display row (``kind='execution'``, the entry the
    cockpit email list and the activity ticker render — one per EMAIL, not per
    row), then the N coverage rows ONE COMMIT PER ROW, each
    IntegrityError-tolerant: a mid-loop failure leaves the earlier rows covered
    and only the remainder re-alertable (minimal duplication under an
    at-least-once posture). Returns True iff an email was sent.
    """
    new_ids = set(candidate_ids) - _covered_rejection_ids(session, set(candidate_ids))
    if not new_ids:
        return False
    rows = list(session.scalars(
        select(ExecutionLog)
        .where(ExecutionLog.id.in_(new_ids),
               ExecutionLog.status.in_(REJECTED_STATUSES))
        .order_by(ExecutionLog.id)))
    if not rows:  # ids that vanished / are not alertable can't compose an honest alert
        return False
    email = compose_live_rejection_alert(rows, run_date)
    send(to=recipient, subject=email.subject, text=email.text, html=email.html,
         attachments=[])  # SEND FIRST (see docstring)
    session.add(EmailLog(sent_at=datetime.now(UTC), kind=_DISPLAY_KIND,
                         subject=email.subject, run_date=run_date,
                         alert_key=_display_key([r.id for r in rows])))
    try:
        session.commit()
    except IntegrityError:  # lost the concurrent-replica race; the row already exists
        session.rollback()
    for row in rows:
        session.add(EmailLog(sent_at=datetime.now(UTC), kind=REJECTION_COVER_KIND,
                             subject=email.subject, run_date=run_date,
                             alert_key=_rejection_key(row.id)))
        try:
            session.commit()
        except IntegrityError:  # lost the concurrent-replica race; the row already exists
            session.rollback()
    return True

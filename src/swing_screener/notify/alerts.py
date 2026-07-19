"""Compose concise, urgent standalone alert emails (no PDF, no digest wait).

Three alert kinds live here:

* EXIT alerts — active real trades whose :class:`ExitEvent` rows fired; the
  ``message`` already carries the ticker and context.
* GUARDRAIL-TRIP alerts (Task 10) — a breaker tripped the live brake; the
  operator must hear it the moment it happens, not in tomorrow's digest.
* LIVE-REJECTION alerts (Task 10) — working live orders the venue rejected or
  canceled (the reconcile's ``rejected_live``/``canceled`` status flips): order
  truth the operator believes is still working but is not. Distinct from an
  exit alert — a venue stop-out is a FILL + CLOSE (position truth), never a
  rejection.

LEAK POSTURE (binding, mirrors ``pipeline.guardrails``): subjects and bodies
are built ONLY from internally formatted strings — breaker names, the repo's
pre-formatted reasons (echoed verbatim, never re-derived), recorded ticket
details — never raw exception text.

Compose functions are pure — no I/O. The ONE exception is the shared
guardrail-trip emitter at the bottom (:func:`send_guardrail_alert` +
:func:`guardrail_alert_sent`): it owns the ``alert_key=str(trip_event_id)``
send-then-log dedup contract for BOTH callers — ``notify.run``'s dispatch loop
and ``pipeline.run``'s evening screen — so the contract has exactly one owner.
It lives HERE because this module stays pipeline-import-free (its only imports
are ``notify.body``, sqlalchemy, and ``db.models``; the send callable is
injected), which lets ``pipeline.run`` import it lazily without ever touching
``notify.run`` (the notify.run -> pipeline.run cycle).
"""

import html
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swing_screener.db.models import EmailLog
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
    """Alert for live orders the venue REJECTED or CANCELED.

    Built from :class:`ExecutionLog` rows the reconcile just flipped to
    ``rejected_live``/``canceled`` — orders the operator believes are working
    but are not (order truth). A venue stop-out is a FILL + CLOSE and rides the
    EXIT alert instead; the two kinds are disjoint by construction. ``status``
    and ``detail`` are internally recorded strings (the leak posture was upheld
    when they were written).
    """
    # Phone-glance subject: count + tickers, so the preview alone says what
    # died. Ticker list bounded (EmailLog.subject is String(256) and Azure SQL
    # enforces it); a digest pass flips at most a handful of same-day orders.
    tickers = [r.ticker for r in rows]
    shown = ", ".join(tickers[:6]) + (", …" if len(tickers) > 6 else "")
    n = len(rows)
    plural = "s" if n != 1 else ""
    subject = (f"Swing Screener — {n} Live Order{plural} Rejected/Canceled: "
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
        EmailLog.kind == "guardrail", EmailLog.alert_key == alert_key
    )
    return session.scalars(stmt).first() is not None


def send_guardrail_alert(session: Session, *, run_date: date, recipient: str,
                         send: Callable[..., None], trip_event_id: int,
                         breaker: str, reason: str) -> bool:
    """Send + log ONE guardrail-trip alert, deduped on ``str(trip_event_id)``.

    The single owner of the trip-alert dedup contract, shared by all three
    callers: ``notify.run._trip_emailer`` (the dispatch loop's in-protocol
    IMMEDIACY half), ``notify.run._emit_pending_guardrail_alert`` (the
    digest-side AT-LEAST-ONCE half), and ``pipeline.run._screen_trip_emailer``
    (the evening screen) — one alert_key, so whichever fires first wins and the
    others no-op. SEND-then-LOG, the ``_emit_pending_exit_alert`` ordering and
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
    session.add(EmailLog(sent_at=datetime.now(UTC), kind="guardrail",
                         subject=email.subject, run_date=run_date, alert_key=key))
    try:
        session.commit()
    except IntegrityError:  # lost the concurrent-replica race; the row already exists
        session.rollback()
    return True

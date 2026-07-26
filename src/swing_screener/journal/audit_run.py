"""The System Behavior Auditor worker: a weekly conduct sweep + a daily breach scan.

``audit_run weekly`` grades the period (compliance + anomaly), writes ONE idempotent
``SystemAudit(kind="weekly")`` row, and (when enabled + under budget) drafts the
independent audit narrative. ``audit_run breach`` scans the trailing week and writes a
``SystemAudit(kind="breach")`` row per hard breach (a cap exceeded, an unexplained
disarm, or one of the four guardrail conduct failures) it hasn't already recorded --
the "flags on the next run after the breach" path (design SS Cadence).

The line between the two halves is deliberate: the brake FIRING -- a trip, its sweep,
the kill switch, a manual HALT, a ``guardrail: ...`` clamp -- is the machine behaving
correctly and is graded as expected conduct in ``audit_compliance``. Only the failures
AROUND the brake (a live submit on a tripped day, an unmailed trip, a stuck sweep, live
orders under no mandate) are breaches here.

Read-only oversight: it writes only its OWN audit rows; it never moves money, changes
config, or disarms. Same code-owns-the-numbers firewall (findings_json authoritative).

FINDINGS SCHEMA (the rendering contract -- ``findings_json`` is authoritative, the
narrative advisory, so a surface reads THIS, never the prose). A ``kind='weekly'`` row
carries ``{compliance: {...}, anomaly: {...}}`` (the two graders' dataclasses); a
``kind='breach'`` row carries exactly ONE of four shapes, told apart by which key is
present:

* ``{"cap_breach": {date, account, notional, risk_dollars, notional_cap, day_r,
  loss_cap_r}}`` -- a hard-limit day.
* ``{"disarm_day": iso, "disarms": [{at, reason, orders_cancelled}]}`` -- unexplained
  disarms (sanctioned guardrail sweeps are filtered out; see ``run_breach_scan``).
* ``{"guardrail_breach": {...}}`` -- the four guardrail conduct rules, whose STABLE
  CORE is ``{rule, day, detail}`` (``rule`` in ``submit-while-tripped`` |
  ``unmailed-trip`` | ``stuck-sweep`` | ``unset-mandate``; ``day`` the ISO day of
  record; ``detail`` a code-owned sentence). Optional ``caveat`` states what the rule
  cannot prove and MUST be surfaced wherever the finding is (rules 1 and 4 carry one).
  Per-rule extras: rule 1 ``n_live_counting, affected_run_dates[], trip_id, trip_day,
  bridge_days, bridge_through, cleared_at|null``; rule 2 ``n_episodes, trips[{id, at,
  breaker, source}]``; rule 3 ``trip_id, sweep_state, trip_reason``; rule 4
  ``n_live_counting, missing_breakers[]``.
* nothing recognised -> the narrative degrades to "Hard breach recorded; see findings."

Treat every key beyond the stable core as OPTIONAL when rendering: a row written by an
older revision will not have the newer ones, and these rows are permanent.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from swing_screener.db.guardrails_repo import (
    INCOMPLETE_SWEEPS,
    GuardrailsState,
    missing_mandate_breakers,
    peek_guardrails,
)
from swing_screener.db.models import (
    AgentGuardrailEvent,
    DisarmEvent,
    EmailLog,
    ExecutionLog,
    SystemAudit,
)
from swing_screener.db.session import get_engine
from swing_screener.journal.audit_anomaly import anomaly_findings
from swing_screener.journal.audit_author import breach_narrative, draft_audit, template_audit
from swing_screener.journal.audit_compliance import compliance_findings, is_sanctioned_disarm
from swing_screener.pipeline.run import _migrate_with_retry, _resolve_db_url
from swing_screener.settings import Settings, load_settings

if TYPE_CHECKING:
    import anthropic

log = logging.getLogger(__name__)
# The ONE authoritative model id for the auditor (2026-07-17 audit: a duplicated
# constant here + a separate default in audit_author could stamp a model the call never
# used). It is passed EXPLICITLY to draft_audit AND stamped on the SystemAudit row, so
# provenance always matches the model actually called. Audit prose is bounded 600-token
# template-grade output (2-4 sentences over code-owned findings), so claude-haiku-4-5
# at $1/$5 per MTok replaces claude-opus-4-8 at $5/$25 -- ~25x cheaper (2026-07-17 cost
# plan). notify.analysis._MODEL_PRICES carries a claude-haiku-4-5 row, so the audit
# spend cap keeps metering. No env override by design; the escape hatch is this line.
_MODEL = "claude-haiku-4-5"

# The breach scan runs weekdays at 16:00 ET, so a today-only window permanently misses
# anything recorded AFTER that day's run (late fills, post-close disarms) and EVERYTHING
# on a weekend or holiday. There is no watermark to derive a tighter window from --
# breach rows are written only when something fired, so a quiet stretch leaves no
# "last scanned" marker -- so each run re-scans the trailing week and leans on the
# per-breach idempotency keys (cap:{day}:{account}, disarm:{day}) to make the overlap
# with prior runs a no-op.
_BREACH_SCAN_LOOKBACK_DAYS = 7


def _breach_scan_window(today: date) -> tuple[date, date]:
    """The inclusive (day_from, day_to) window one breach-scan run covers."""
    return today - timedelta(days=_BREACH_SCAN_LOOKBACK_DAYS), today


#: the COUNTING statuses that are LIVE money -- the live half of
#: ``repo.LIMIT_COUNTING_STATUSES`` (a test pins the subset relation). ``skipped`` /
#: ``rejected_live`` / ``canceled`` never reserved anything: a ``guardrail: ...`` clamp
#: row IS the brake working, and counting it would flag exactly the correct days.
_LIVE_COUNTING_STATUSES = ("submitted_live", "filled_live")

#: how many calendar days after a ``run_date`` the submit-while-tripped rule still
#: bridges to a trip event's wall clock. FOUR: the digest dispatches orders stamped
#: with the PRIOR session's run_date, so the routine offset is +1 -- +4 is bounded
#: headroom for weekend/holiday offsets (a Friday run_date dispatched the following
#: Tuesday), deliberately small so the ordering caveat stays meaningful rather than
#: swallowing whole weeks. See ``_submit_while_tripped``'s "two clocks".
_RUN_DATE_BRIDGE_DAYS = 4

#: the trip-alert EmailLog kind (``notify.alerts.send_guardrail_alert`` logs
#: ``kind=TRIP_ALERT_KIND, alert_key=str(trip_event_id)``). Restated as a literal for
#: the same reason ``audit_compliance`` restates the bookkeeping kind: the auditor does
#: not take a notify edge for one string. ``notify.alerts.TRIP_ALERT_KIND`` is the
#: writer and therefore the owner; tests/journal/test_audit_run.py pins the two so they
#: can never drift. The kind filter is also the Task-11 KIND HYGIENE guarantee -- an
#: ``execution-cover`` bookkeeping row can never satisfy a trip alert.
_TRIP_ALERT_KIND = "guardrail"


@dataclass(frozen=True)
class _GuardrailBreach:
    """One candidate guardrail conduct breach: its day of record, idempotency key,
    severity and code-owned findings (``breach_narrative`` renders the prose)."""

    day: date
    key: str
    severity: str
    findings: dict


def _collect(session: Session, *, settings: Settings, period_from: date,
             period_to: date) -> tuple[dict, str]:
    """Grade the period and return (findings dict, severity)."""
    comp = compliance_findings(
        session, period_from=period_from, period_to=period_to,
        max_daily_notional=settings.max_daily_notional,
        max_daily_loss=settings.max_daily_loss)
    anom = anomaly_findings(session, period_from=period_from, period_to=period_to)
    findings = {"compliance": asdict(comp), "anomaly": asdict(anom)}
    if comp.cap_breaches:
        severity = "alert"
    elif anom.drought_days or anom.orphan_exit_events or comp.n_unexplained_disarms:
        # ``n_unexplained_disarms``, NOT ``n_disarms`` (Task 14): a trip sweep, a kill
        # switch or a manual HALT is the brake WORKING -- expected conduct, graded info
        # and counted in the facts. Only a disarm the guardrails machinery did not
        # author still escalates the week.
        severity = "warn"
    else:
        severity = "info"
    return findings, severity


def _worth_narrating(findings: dict) -> bool:
    """True when the week has something an operator should actually read. The gate that
    keeps an ENABLED auditor from spending an LLM call to narrate a dead week: a clean
    week still WRITES the weekly row (deterministic template narrative), just at $0."""
    comp = findings.get("compliance", {})
    anom = findings.get("anomaly", {})
    if (
        comp.get("cap_breaches")
        or comp.get("n_disarms")
        # a trip week is worth reading even when the sweep's DisarmEvent never landed
        # (its write is best-effort by design -- the sweep outranks its own journal).
        or comp.get("n_guardrail_trips")
        or comp.get("n_rejected")
        or anom.get("drought_days")
        or anom.get("would_surface_leaks")
        or anom.get("orphan_exit_events")
    ):
        return True
    # the analyst's nudges measurably hurting (>=3 scored, mean R < 0) is worth a look.
    nudge = (anom.get("calibration") or {}).get("nudge_vs_baseline_r")
    return bool(nudge and len(nudge) == 2 and nudge[0] >= 3 and nudge[1] < 0)


def _get_audit(session: Session, *, kind: str, period_from: date, period_to: date,
               breach_key: str) -> SystemAudit | None:
    return session.scalars(
        select(SystemAudit).where(
            SystemAudit.kind == kind, SystemAudit.period_from == period_from,
            SystemAudit.period_to == period_to, SystemAudit.breach_key == breach_key)
    ).first()


def run_weekly(
    session: Session, *, settings: Settings, period_from: date, period_to: date,
    now: datetime, client: anthropic.Anthropic | None = None,
) -> SystemAudit:
    """Grade the week and upsert the single weekly audit row (idempotent on the period)."""
    findings, severity = _collect(
        session, settings=settings, period_from=period_from, period_to=period_to)

    audit = _get_audit(session, kind="weekly", period_from=period_from,
                       period_to=period_to, breach_key="")
    if audit is None:
        audit = SystemAudit(kind="weekly", period_from=period_from, period_to=period_to,
                            breach_key="")
        session.add(audit)
    audit.findings_json = json.dumps(findings)
    audit.severity = severity
    audit.generated_at = now

    # Even when enabled, don't pay to narrate a dead week -- a clean period gets the
    # deterministic template at $0; the LLM only runs when there's something to report.
    want_llm = (
        settings.audit_enabled
        and settings.audit_max_usd != 0
        and _worth_narrating(findings)
    )
    if want_llm:
        draft = draft_audit(findings, client=client, model=_MODEL)
        audit.narrative = draft.text
        if draft.usage is not None:
            audit.model = _MODEL
            audit.input_tokens = draft.usage.input_tokens
            audit.output_tokens = draft.usage.output_tokens
            audit.est_cost_usd = draft.usage.est_cost_usd
    else:
        audit.narrative = template_audit(findings)

    session.commit()
    session.refresh(audit)
    return audit


def run_breach_scan(
    session: Session, *, settings: Settings, day_from: date, day_to: date, now: datetime,
) -> list[SystemAudit]:
    """Write a breach audit row per NEW hard breach in the window (idempotent). Returns
    the rows written this run.

    Three rule families, each with its own day-keyed idempotency key:
    ``cap:{day}:{account}``, ``disarm:{day}`` (unexplained disarms only), and the four
    ``guardrail-*:{day}`` conduct rules (see ``_guardrail_breaches``)."""
    written: list[SystemAudit] = []

    # cap breaches (per-day, from the compliance grader)
    comp = compliance_findings(
        session, period_from=day_from, period_to=day_to,
        max_daily_notional=settings.max_daily_notional,
        max_daily_loss=settings.max_daily_loss)
    for breach in comp.cap_breaches:
        day = date.fromisoformat(str(breach["date"]))
        # breaches are graded per (day, account); the account belongs in the idempotency
        # key or a second account's same-day breach would be silently swallowed.
        key = f"cap:{day.isoformat()}:{breach['account']}"
        if _get_audit(session, kind="breach", period_from=day, period_to=day,
                      breach_key=key) is not None:
            continue
        written.append(_write_breach(session, day=day, breach_key=key, severity="alert",
                                     findings={"cap_breach": breach}, now=now))

    # disarms (one breach row per disarm day, carrying that day's actual events so the
    # narrative and findings_json state what happened, not a day with no detail).
    # SANCTIONED sweeps are filtered out (Task 14): a trip's own sweep, the kill switch
    # and the manual HALT are the brake working -- the operator already heard (trip
    # email / the action they took), and an alert row per correct brake firing is
    # exactly the cry-wolf that makes a conduct record unreadable. They are counted in
    # the weekly facts instead (``compliance.n_guardrail_sweeps`` and siblings), and
    # the FAILURES around them are the four guardrail rules below.
    disarms_by_day: dict[date, list[DisarmEvent]] = {}
    for event in session.scalars(
        select(DisarmEvent).where(
            DisarmEvent.created_at >= datetime.combine(day_from, time.min),
            DisarmEvent.created_at <= datetime.combine(day_to, time.max))):
        if is_sanctioned_disarm(event.reason or ""):
            continue
        disarms_by_day.setdefault(event.created_at.date(), []).append(event)
    for day in sorted(disarms_by_day):
        key = f"disarm:{day.isoformat()}"
        if _get_audit(session, kind="breach", period_from=day, period_to=day,
                      breach_key=key) is not None:
            continue
        findings = {
            "disarm_day": day.isoformat(),
            "disarms": [
                {"at": e.created_at.isoformat(), "reason": e.reason,
                 "orders_cancelled": e.orders_cancelled}
                for e in sorted(disarms_by_day[day], key=lambda e: e.created_at)
            ],
        }
        written.append(_write_breach(session, day=day, breach_key=key, severity="alert",
                                     findings=findings, now=now))

    # guardrail conduct failures (Task 14) -- the brake's four HARD breaches.
    for gb in _guardrail_breaches(session, day_from=day_from, day_to=day_to, now=now):
        if _get_audit(session, kind="breach", period_from=gb.day, period_to=gb.day,
                      breach_key=gb.key) is not None:
            continue
        written.append(_write_breach(session, day=gb.day, breach_key=gb.key,
                                     severity=gb.severity, findings=gb.findings, now=now))

    if written:
        session.commit()
    return written


# ------------------------------------------------------------ guardrail breach rules


def _guardrail_breaches(
    session: Session, *, day_from: date, day_to: date, now: datetime
) -> list[_GuardrailBreach]:
    """The four guardrail CONDUCT-FAILURE rules, day-keyed like ``disarm:{day}``.

    Everything the brake does correctly is graded in ``audit_compliance`` as expected
    conduct; this is the other half -- what it means for the machine to have gotten
    the brake WRONG:

    1. ``guardrail-submit-while-tripped:{day}`` (warn) -- counting live orders whose
       trading day of record meets a trip still in force (the run_date bridge). ONE row
       per in-force TRIP, keyed on the earliest run_date it covers.
    2. ``guardrail-unmailed-trip:{day}`` (alert) -- a trip EPISODE the operator was
       never mailed about, keyed on the episode's first trip.
    3. ``guardrail-stuck-sweep:{day}`` (alert) -- a trip's sweep unfinished for a day.
    4. ``guardrail-unset-mandate:{day}`` (warn) -- live orders while any mandatory
       breaker is unset.

    Rules 1 and 2 group by the thing that went wrong (the trip, the episode) rather
    than by the day it touched, so ONE failure is ONE permanent row however many days
    or scans it spans. Rule 4 stays per-day: the mandate is read at scan time and each
    trading day of record is its own statement about that day's live orders.

    DAY KEY: the trading day of record. Execution rows key on ``run_date`` (the digest
    stamps ``repo.latest_run_date`` there and the breakers count on it -- the Task-11
    day-key convention), event-derived rows key on the event's own day, exactly as the
    ``disarm:{day}`` rule does. No ``date.today()`` anywhere: the caller's ``now`` is
    the only clock, so a re-scan of an old window can never drift.

    READ-ONLY: the brake state is ``peek_guardrails`` (never ``load_guardrails``) --
    the Auditor must not seed the agent_guardrails row it is auditing.

    ONE TIMELINE, read once (``_load_timeline``) and shared by every rule that walks
    it: the per-rule queries this replaces re-read the same append-only table up to a
    dozen times per scan AND could order it differently, which is how a 'clear' once
    counted as following a trip it actually preceded.
    """
    g = peek_guardrails(session)
    timeline = _load_timeline(session)
    live_days = _live_counting_days(session, day_from=day_from, day_to=day_to)
    return [
        *_submit_while_tripped(timeline, live_days=live_days),
        *_unmailed_trips(session, timeline, day_from=day_from, day_to=day_to),
        *_stuck_sweep(session, g=g, now=now),
        *_unset_mandate(g=g, live_days=live_days),
    ]


def _live_counting_days(
    session: Session, *, day_from: date, day_to: date
) -> dict[date, int]:
    """``{trading day of record: n counting live rows}`` over the window.

    Windowed AND keyed on ``run_date`` -- the trading day of record the guardrail
    breakers themselves count on -- so a row recorded late still grades on the session
    it belongs to (the trailing-7d re-scan is what catches it)."""
    rows = session.execute(
        select(ExecutionLog.run_date, func.count())
        .where(
            ExecutionLog.status.in_(_LIVE_COUNTING_STATUSES),
            ExecutionLog.run_date >= day_from,
            ExecutionLog.run_date <= day_to,
        )
        .group_by(ExecutionLog.run_date)
    )
    return {day: int(n) for day, n in rows}


def _naive(dt: datetime) -> datetime:
    """``dt`` as a naive UTC datetime, so timeline walks can never raise mid-scan.

    Both backends hand these timestamps back WITHOUT tzinfo (sqlite's DATETIME string
    format and SQL Server's DATETIME column both drop the offset the writer passed),
    and the horizons this module builds with ``datetime.combine`` are naive too. This
    normalises anyway: an aware value -- a future backend, a caller's ``now`` -- is
    converted to UTC and stripped rather than raising TypeError against a naive bound.
    """
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


@dataclass(frozen=True)
class _GuardrailEvent:
    """One state-moving guardrail event off the shared timeline: its id (a trip's id IS
    its alert_key), when it landed, and which breaker/emitter recorded it."""

    id: int
    created_at: datetime
    kind: str
    breaker: str
    source: str


def _load_timeline(session: Session) -> list[_GuardrailEvent]:
    """The trip/clear history, ONCE per scan, in ONE canonical order.

    Every rule that reasons about brake state walks this list rather than issuing its
    own query: one read instead of a dozen over the same append-only table, and -- the
    correctness half -- ONE ordering, so "after" means the same thing to every rule.
    Ordered by ``(created_at, id)``: the append sequence breaks ties when two processes
    stamp the same instant. 'halt' is deliberately excluded: it can neither set nor
    release trippedness (``halt()`` requires state 'ok' and a trip overwrites 'halted'),
    so including it could only corrupt the walk. Unbounded by design -- an open episode
    routinely starts before the scan window, and this table is low volume."""
    return [
        _GuardrailEvent(id=event_id, created_at=created_at, kind=kind,
                        breaker=breaker, source=source)
        for event_id, created_at, kind, breaker, source in session.execute(
            select(AgentGuardrailEvent.id, AgentGuardrailEvent.created_at,
                   AgentGuardrailEvent.kind, AgentGuardrailEvent.breaker,
                   AgentGuardrailEvent.source)
            .where(AgentGuardrailEvent.kind.in_(("trip", "clear")))
            .order_by(AgentGuardrailEvent.created_at, AgentGuardrailEvent.id)
        )
    ]


def _trip_in_force_through(
    timeline: list[_GuardrailEvent], through: date
) -> _GuardrailEvent | None:
    """The trip still IN FORCE at the end of ``through``, or None if the brake was free.

    Walks the shared timeline up to that instant and keeps the last trip no later
    'clear' released. Reconstructed from events rather than read off the guardrails row
    because the row carries only the state NOW, while a breach scan grades days that
    have already ended."""
    horizon = datetime.combine(through, time.max)
    in_force: _GuardrailEvent | None = None
    for event in timeline:
        if _naive(event.created_at) > horizon:
            break  # the timeline is ordered; everything past here is later still
        in_force = event if event.kind == "trip" else None
    return in_force


def _cleared_at(
    timeline: list[_GuardrailEvent], trip: _GuardrailEvent
) -> datetime | None:
    """When ``trip`` was released, if it had been by scan time -- FULL timeline.

    Deliberately unbounded, unlike ``_trip_in_force_through``'s bridge-window walk: the
    breach row it annotates is PERMANENT, so it must not imply "never cleared" when all
    that was read was a four-day window. "After" means after in the timeline's ONE
    ordering (``created_at`` then id), not "a bigger id" -- two processes clock-skewed
    by a second could otherwise make a clear look like it preceded the trip it
    released. None means no clear had landed by scan time; it is a write-once snapshot,
    not a claim that none ever will."""
    seen = False
    for event in timeline:
        if event.id == trip.id:
            seen = True
        elif seen and event.kind == "clear":
            return event.created_at
    return None


def _submit_while_tripped(
    timeline: list[_GuardrailEvent], *, live_days: dict[date, int]
) -> list[_GuardrailBreach]:
    """RULE 1 (warn): counting live orders whose run_date meets a trip still in force.

    THE TWO CLOCKS -- read this before tightening the rule. ``ExecutionLog.run_date``
    is the TRADING DAY OF RECORD (the evening screen mints it; the digest stamps it on
    the orders it dispatches the NEXT morning), while a guardrail event carries a
    wall-clock ``created_at``. Matching the two exactly compares different clocks and
    makes the rule structurally INERT on the only path that submits live orders:
    Friday-stamped orders are dispatched Monday, and Monday's trip event is Monday, so
    an equality test finds nothing on a book that plainly breached.

    So the rule BRIDGES: for a run_date R carrying counting live rows, it asks whether
    a trip was in force -- tripped and not released by a 'clear' WITHIN THE WINDOW IT
    READS -- at any point through ``R + _RUN_DATE_BRIDGE_DAYS``, which spans the
    run_date -> dispatch-day offset even across a weekend. A trip predating R and still
    unreleased counts too (the brake was in force the whole time).

    The bridge's cost is ORDERING, and it is admitted rather than hidden: within that
    span the auditor cannot prove a submission FOLLOWED the trip rather than preceding
    it (there is no submit timestamp to compare, and the trip may be days after the
    stamped session). So the finding is filed **warn, never alert**, with the caveat
    stated verbatim in the narrative. A clear landing AFTER the bridge window is
    invisible to the in-force walk, so the row never claims "never cleared": the detail
    scopes its claim to the window, and ``cleared_at`` (``_cleared_at``, full timeline)
    carries the release when one exists. The provable, alert-grade version of this signal
    is the submit-side clamp itself: a blocked order writes a ``guardrail: ...``
    skipped row (counted as expected conduct), so a day of correct braking shows clamps
    and NO counting live rows.
    """
    # GROUP BY TRIP, not by day: one trip is ONE conduct failure however many trading
    # days of record its bridge covers, so a trip spanning a Friday and the Monday it
    # was dispatched into files one permanent row, not one per day (the same cry-wolf
    # the unmailed rule avoids by keying on the episode).
    by_trip: dict[int, _GuardrailEvent] = {}
    days_by_trip: dict[int, list[date]] = {}
    for day in sorted(live_days):
        in_force = _trip_in_force_through(
            timeline, day + timedelta(days=_RUN_DATE_BRIDGE_DAYS))
        if in_force is None:
            continue
        by_trip[in_force.id] = in_force
        days_by_trip.setdefault(in_force.id, []).append(day)

    out: list[_GuardrailBreach] = []
    for trip_id, days in days_by_trip.items():
        trip = by_trip[trip_id]
        n = sum(live_days[day] for day in days)
        # the FURTHEST point the in-force walk verified for this trip: the walk is
        # monotone (no clear through the latest day's bridge end implies none through
        # any earlier one), so the latest bridge end is what the detail may claim.
        bridge_through = max(days) + timedelta(days=_RUN_DATE_BRIDGE_DAYS)
        # what the WINDOW saw is "no clear through bridge_through"; what is TRUE at
        # scan time may be a later clear. Both go on the row -- the detail claims only
        # the former, ``cleared_at`` carries the latter.
        cleared = _cleared_at(timeline, trip)
        out.append(_GuardrailBreach(
            day=min(days),  # keyed on the EARLIEST affected trading day of record
            key=f"guardrail-submit-while-tripped:{min(days).isoformat()}",
            severity="warn",
            findings={"guardrail_breach": {
                "rule": "submit-while-tripped", "day": min(days).isoformat(),
                "n_live_counting": n,
                "affected_run_dates": [d.isoformat() for d in days],
                "trip_id": trip_id, "trip_day": trip.created_at.date().isoformat(),
                "bridge_days": _RUN_DATE_BRIDGE_DAYS,
                "bridge_through": bridge_through.isoformat(),
                "cleared_at": cleared.isoformat() if cleared is not None else None,
                "detail": (f"{n} counting live order(s) stamped across "
                           f"{len(days)} trading day(s) of record with trip {trip_id} "
                           f"({trip.created_at.date().isoformat()}) in force during "
                           f"the bridge window, not cleared within it (through "
                           f"{bridge_through.isoformat()})"),
                "caveat": ("run_date is the trading day of record, not a wall clock, "
                           "and execution rows carry no submit time -- the auditor "
                           "cannot prove these orders were submitted after the trip "
                           "rather than before it"),
            }}))
    return out


@dataclass(frozen=True)
class _TripEpisode:
    """One breach the operator has not released yet -- every trip event the racing
    processes appended for it, plus whether a 'clear' closed it."""

    trips: list[_GuardrailEvent]
    closed_by_clear: bool


def _trip_episodes(timeline: list[_GuardrailEvent]) -> list[_TripEpisode]:
    """The shared timeline as EPISODES.

    An episode is a maximal run of ``kind='trip'`` events with no intervening
    ``kind='clear'`` -- i.e. ONE breach the operator has not yet released, however many
    trip events it produced. That is the honest unit because
    ``guardrails_repo.trip()`` appends its event UNCONDITIONALLY, BEFORE the
    rows-affected election: on a concurrent breach every racing process (digest,
    screen, exitcheck, cockpit) leaves a trip event, but only the WINNER runs the
    response, so only the winner's id can ever carry an alert. Grading per EVENT would
    permanently flag the losers, whose silence is by design.
    """
    episodes: list[_TripEpisode] = []
    current: list[_GuardrailEvent] = []
    for event in timeline:
        if event.kind == "clear":
            if current:
                episodes.append(_TripEpisode(trips=current, closed_by_clear=True))
                current = []
        else:
            current.append(event)
    if current:
        episodes.append(_TripEpisode(trips=current, closed_by_clear=False))
    return episodes


def _unmailed_trips(
    session: Session, timeline: list[_GuardrailEvent], *, day_from: date, day_to: date
) -> list[_GuardrailBreach]:
    """RULE 2 (alert): a trip EPISODE with no ``EmailLog(kind='guardrail')`` on ANY of
    its trip ids.

    The trip alert is the operator's ONLY real-time signal that the machine braked
    itself, so an episode with no alert row means the immediacy contract was missed --
    graded alert even though the hourly retry emitter may close the gap minutes later
    (the row records that it was missed, and the retry makes it a one-off).

    ONE EPISODE, ONE REQUIRED EMAIL (see ``_trip_episodes``): the election loser's trip
    event stays permanently unmailed by design, and the winner's alert covers the
    breach they both saw, so any mailed id in the episode discharges the contract.

    EXEMPTION -- an episode a 'clear' closed is never flagged: Task 10's emitter
    deliberately never mails a cleared trip because clearing is an operator action at
    the cockpit and therefore proves awareness. Note this exemption is exact rather
    than approximate: a HALT release also writes 'clear', but it can never land between
    a trip and its ack (``clear_halt`` requires state 'halted' and a trip overwrites
    'halted'), so every 'clear' following a trip IS that trip's acknowledgement.
    """
    # kind='guardrail' ONLY: the per-row 'execution-cover' bookkeeping rows are not
    # sent emails and must never satisfy a trip's alert contract (Task-11 hygiene).
    # NOT date-filtered, mirroring ``alerts.guardrail_alert_sent``: the retry owner
    # may mail an evening trip on the NEXT morning's run, and a date filter here would
    # flag exactly that (correct) late delivery as unmailed.
    mailed = set(session.scalars(
        select(EmailLog.alert_key).where(EmailLog.kind == _TRIP_ALERT_KIND)
    ))
    by_day: dict[date, list[_TripEpisode]] = {}
    for episode in _trip_episodes(timeline):
        if episode.closed_by_clear:
            continue  # the operator cleared it -> awareness (see the docstring)
        if not any(day_from <= t.created_at.date() <= day_to for t in episode.trips):
            continue  # nothing of this episode happened in the scanned window
        if any(str(t.id) in mailed for t in episode.trips):
            continue  # one alert covers the whole episode
        # KEY ON THE EPISODE, NOT THE WINDOW: an open episode whose trips straddle two
        # scans (07-08 and 07-10) would otherwise key on whichever of its trips the
        # current window happened to include, and the daily re-scan would file the SAME
        # episode again under a second day. The episode's earliest trip is intrinsic to
        # it, so the idempotency key is stable however the window slides.
        by_day.setdefault(episode.trips[0].created_at.date(), []).append(episode)
    out: list[_GuardrailBreach] = []
    for day in sorted(by_day):
        episodes = by_day[day]
        flat = [t for episode in episodes for t in episode.trips]
        out.append(_GuardrailBreach(
            day=day, key=f"guardrail-unmailed-trip:{day.isoformat()}", severity="alert",
            findings={"guardrail_breach": {
                "rule": "unmailed-trip", "day": day.isoformat(),
                "n_episodes": len(episodes),
                # ids live in ``trips`` ONLY -- a permanent record should not state the
                # same fact twice and risk the two copies disagreeing after an edit.
                "trips": [{"id": t.id, "at": t.created_at.isoformat(),
                           "breaker": t.breaker, "source": t.source} for t in flat],
                "detail": (f"{len(episodes)} trip episode(s) ({len(flat)} trip "
                           f"event(s)) with no alert email logged for any of their "
                           f"trip ids, and no clear"),
            }}))
    return out


def _stuck_sweep(
    session: Session, *, g: GuardrailsState, now: datetime
) -> list[_GuardrailBreach]:
    """RULE 3 (alert): a trip's sweep still pending/partial a full day later.

    Every cycle (digest, evening screen, hourly exit check) re-runs an incomplete sweep
    via ``resume_incomplete_sweep``, so a sweep that has survived a whole day of retries
    is not slow -- it is stuck, and the book may hold unprotected live exposure. Keyed
    on the TRIP's day (its state is a fact about NOW, so it is deliberately not window
    filtered: a sweep stuck since before the window still gets recorded once)."""
    if (g.state != "tripped" or g.sweep_state not in INCOMPLETE_SWEEPS
            or g.trip_id is None):
        return []
    trip = session.get(AgentGuardrailEvent, g.trip_id)
    if trip is None:  # defensive: no trip event -> no honest day to key on
        log.error("guardrails row references trip event %d that does not exist -- "
                  "cannot day-key a stuck-sweep breach", g.trip_id)
        return []
    day = trip.created_at.date()
    if (now.date() - day).days < 1:
        return []  # today's sweep still belongs to the resume owner, not the auditor
    return [_GuardrailBreach(
        day=day, key=f"guardrail-stuck-sweep:{day.isoformat()}", severity="alert",
        findings={"guardrail_breach": {
            "rule": "stuck-sweep", "day": day.isoformat(), "trip_id": g.trip_id,
            "sweep_state": g.sweep_state, "trip_reason": g.trip_reason,
            # "stuck since {day}", not "{n} day(s) later": the row is written once and
            # read forever, so an age computed at write time would freeze and mislead.
            "detail": (f"trip {g.trip_id}'s disarm sweep has been "
                       f"'{g.sweep_state}' since {day.isoformat()}, through every "
                       f"cycle's resume"),
        }})]


def _unset_mandate(
    *, g: GuardrailsState, live_days: dict[date, int]
) -> list[_GuardrailBreach]:
    """RULE 4 (warn): counting live orders while ANY mandatory breaker is unset.

    ANY, not all three: the mandate is a completeness rule
    (``guardrails_repo.missing_mandate_breakers`` -- the SAME list the submit-side
    check refuses on), and the realistic misconfiguration is exactly one breaker
    forgotten. Requiring all three to be unset would let the likely case through
    unrecorded while flagging only the case nobody hits.

    Reuses the mandate's own breaker list but NOT its ``state != 'ok'`` branch: a
    tripped brake is not a misconfigured one, and rule 1 owns tripped-state conduct.

    The submit-side mandate already refuses real money without the three breakers, so
    this is the conduct-record backstop, not the enforcement: it says the live book
    moved while the brake was not fully configured to catch it. Warn, not alert,
    because the auditor cannot tell a real-money endpoint from a paper-host drill (the
    broker host is env, never DB) and the mandate check is what actually gates real
    money. Evaluated against the breakers AS SET NOW -- an operator who set them
    afterwards leaves no way to reconstruct the day's configuration."""
    missing = missing_mandate_breakers(g)
    if not missing:
        return []
    named = ", ".join(missing)
    out: list[_GuardrailBreach] = []
    for day in sorted(live_days):
        n = live_days[day]
        out.append(_GuardrailBreach(
            day=day, key=f"guardrail-unset-mandate:{day.isoformat()}", severity="warn",
            findings={"guardrail_breach": {
                "rule": "unset-mandate", "day": day.isoformat(), "n_live_counting": n,
                "missing_breakers": missing,
                "detail": (f"{n} counting live order(s) with mandatory breaker(s) "
                           f"unset: {named}"),
                "caveat": ("the auditor reads no broker host: a paper-endpoint drill "
                           "records identically to real money here"),
            }}))
    return out


def _write_breach(session: Session, *, day: date, breach_key: str, severity: str,
                  findings: dict, now: datetime) -> SystemAudit:
    row = SystemAudit(kind="breach", period_from=day, period_to=day, breach_key=breach_key,
                      findings_json=json.dumps(findings), severity=severity,
                      narrative=breach_narrative(findings), generated_at=now)
    session.add(row)
    session.flush()  # assign an id without ending the batch txn
    return row


def _build_client() -> anthropic.Anthropic | None:
    from swing_screener.config_secrets import get_secret  # noqa: PLC0415
    try:
        import anthropic  # noqa: PLC0415

        return anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
    except Exception:  # noqa: BLE001
        log.warning("audit client unavailable; narrative will use the deterministic template")
        return None


def main() -> None:
    """`python -m swing_screener.journal.audit_run {weekly|breach}` -- DB-writing ACA Job."""
    settings = load_settings()
    parser = argparse.ArgumentParser(description="System Behavior Auditor sweeps.")
    parser.add_argument("--db", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("weekly", help="grade the trailing week + write the weekly audit")
    sub.add_parser(
        "breach",
        help="scan the trailing week for hard breaches (caps, disarms, guardrail conduct)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    db_url = _resolve_db_url(args.db)
    if db_url.startswith("mssql"):
        _migrate_with_retry(db_url)
    engine = get_engine(db_url)
    now = datetime.now(UTC)
    today = now.date()
    with Session(engine) as session:
        if args.cmd == "weekly":
            client = _build_client() if settings.audit_enabled else None
            audit = run_weekly(session, settings=settings, period_from=today - timedelta(days=6),
                               period_to=today, now=now, client=client)
            log.info("audit weekly: severity=%s", audit.severity)
        else:
            day_from, day_to = _breach_scan_window(today)
            rows = run_breach_scan(session, settings=settings, day_from=day_from,
                                   day_to=day_to, now=now)
            log.info("audit breach: wrote=%d", len(rows))


if __name__ == "__main__":
    main()

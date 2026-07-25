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

from swing_screener.db.guardrails_repo import GuardrailsState, peek_guardrails
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

#: sweep_state values that mean the trip's sweep never finished.
_INCOMPLETE_SWEEPS = ("pending", "partial")

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
       trading day of record meets a trip still in force (the run_date bridge).
    2. ``guardrail-unmailed-trip:{day}`` (alert) -- a trip EPISODE the operator was
       never mailed about.
    3. ``guardrail-stuck-sweep:{day}`` (alert) -- a trip's sweep unfinished for a day.
    4. ``guardrail-unset-mandate:{day}`` (warn) -- live orders with no mandatory
       breakers set.

    DAY KEY: the trading day of record. Execution rows key on ``run_date`` (the digest
    stamps ``repo.latest_run_date`` there and the breakers count on it -- the Task-11
    day-key convention), event-derived rows key on the event's own day, exactly as the
    ``disarm:{day}`` rule does. No ``date.today()`` anywhere: the caller's ``now`` is
    the only clock, so a re-scan of an old window can never drift.

    READ-ONLY: the brake state is ``peek_guardrails`` (never ``load_guardrails``) --
    the Auditor must not seed the agent_guardrails row it is auditing.
    """
    g = peek_guardrails(session)
    live_days = _live_counting_days(session, day_from=day_from, day_to=day_to)
    return [
        *_submit_while_tripped(session, live_days=live_days),
        *_unmailed_trips(session, day_from=day_from, day_to=day_to),
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


def _trip_in_force_through(
    session: Session, through: date
) -> tuple[int, datetime] | None:
    """The ``(id, created_at)`` of a trip still IN FORCE at the end of ``through``.

    Walks the trip/clear timeline in order and returns the last trip no later 'clear'
    released (None when the brake was free). Reconstructed from events rather than read
    off the guardrails row because the row carries only the state NOW, while a breach
    scan grades days that have already ended. 'halt' is deliberately not consulted: it
    cannot release a trip (``halt()`` requires state 'ok', and a trip overwrites
    'halted'), so it can neither set nor clear trippedness."""
    in_force: tuple[int, datetime] | None = None
    for event_id, created_at, kind in session.execute(
        select(AgentGuardrailEvent.id, AgentGuardrailEvent.created_at,
               AgentGuardrailEvent.kind)
        .where(
            AgentGuardrailEvent.kind.in_(("trip", "clear")),
            AgentGuardrailEvent.created_at <= datetime.combine(through, time.max),
        )
        .order_by(AgentGuardrailEvent.created_at, AgentGuardrailEvent.id)
    ):
        in_force = (event_id, created_at) if kind == "trip" else None
    return in_force


def _cleared_at(session: Session, *, trip_id: int) -> datetime | None:
    """When the named trip was released, if it ever was -- over the FULL timeline.

    Deliberately UNBOUNDED, unlike ``_trip_in_force_through``'s bridge-window walk: the
    breach row it annotates is PERMANENT, so it must not imply "never cleared" when all
    it read was a four-day window. A clear that landed after the window is exactly the
    context an operator needs when they open the row later. Ordered by id (the append
    sequence), so the FIRST clear after the trip is its release."""
    return session.scalars(
        select(AgentGuardrailEvent.created_at)
        .where(AgentGuardrailEvent.kind == "clear", AgentGuardrailEvent.id > trip_id)
        .order_by(AgentGuardrailEvent.id)
        .limit(1)
    ).first()


def _submit_while_tripped(
    session: Session, *, live_days: dict[date, int]
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
    out: list[_GuardrailBreach] = []
    for day in sorted(live_days):
        bridge_end = day + timedelta(days=_RUN_DATE_BRIDGE_DAYS)
        in_force = _trip_in_force_through(session, bridge_end)
        if in_force is None:
            continue
        trip_id, trip_at = in_force
        n = live_days[day]
        # what the WINDOW saw is "no clear through bridge_end"; what is TRUE at scan
        # time may be a later clear. Both go on the row -- the detail claims only the
        # former, ``cleared_at`` carries the latter (None when genuinely uncleared).
        cleared = _cleared_at(session, trip_id=trip_id)
        out.append(_GuardrailBreach(
            day=day, key=f"guardrail-submit-while-tripped:{day.isoformat()}",
            severity="warn",
            findings={"guardrail_breach": {
                "rule": "submit-while-tripped", "day": day.isoformat(),
                "n_live_counting": n, "trip_id": trip_id,
                "trip_day": trip_at.date().isoformat(),
                "bridge_days": _RUN_DATE_BRIDGE_DAYS,
                "cleared_at": cleared.isoformat() if cleared is not None else None,
                "detail": (f"{n} counting live order(s) stamped for this trading day "
                           f"with trip {trip_id} ({trip_at.date().isoformat()}) in "
                           f"force during the bridge window, not cleared within it "
                           f"(through {bridge_end.isoformat()})"),
                "caveat": ("run_date is the trading day of record, not a wall clock, "
                           "and execution rows carry no submit time -- the auditor "
                           "cannot prove these orders were submitted after the trip "
                           "rather than before it"),
            }}))
    return out


@dataclass(frozen=True)
class _TripEvent:
    """One appended trip event: its id (which IS the alert_key), when it landed, and
    which breaker/emitter saw the breach."""

    id: int
    created_at: datetime
    breaker: str
    source: str


@dataclass(frozen=True)
class _TripEpisode:
    """One breach the operator has not released yet -- every trip event the racing
    processes appended for it, plus whether a 'clear' closed it."""

    trips: list[_TripEvent]
    closed_by_clear: bool


def _trip_episodes(session: Session) -> list[_TripEpisode]:
    """The trip timeline as EPISODES.

    An episode is a maximal run of ``kind='trip'`` events with no intervening
    ``kind='clear'`` -- i.e. ONE breach the operator has not yet released, however many
    trip events it produced. That is the honest unit because
    ``guardrails_repo.trip()`` appends its event UNCONDITIONALLY, BEFORE the
    rows-affected election: on a concurrent breach every racing process (digest,
    screen, exitcheck, cockpit) leaves a trip event, but only the WINNER runs the
    response, so only the winner's id can ever carry an alert. Grading per EVENT would
    permanently flag the losers, whose silence is by design.

    Reads the whole timeline, unbounded: an episode routinely starts before the scan
    window (the trip that is still open), and ``agent_guardrail_events`` is a
    low-volume append-only table -- two kinds of it is a small scan.
    """
    episodes: list[_TripEpisode] = []
    current: list[_TripEvent] = []
    for event_id, created_at, kind, breaker, source in session.execute(
        select(AgentGuardrailEvent.id, AgentGuardrailEvent.created_at,
               AgentGuardrailEvent.kind, AgentGuardrailEvent.breaker,
               AgentGuardrailEvent.source)
        .where(AgentGuardrailEvent.kind.in_(("trip", "clear")))
        .order_by(AgentGuardrailEvent.created_at, AgentGuardrailEvent.id)
    ):
        if kind == "clear":
            if current:
                episodes.append(_TripEpisode(trips=current, closed_by_clear=True))
                current = []
        else:
            current.append(_TripEvent(id=event_id, created_at=created_at,
                                      breaker=breaker, source=source))
    if current:
        episodes.append(_TripEpisode(trips=current, closed_by_clear=False))
    return episodes


def _unmailed_trips(
    session: Session, *, day_from: date, day_to: date
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
    for episode in _trip_episodes(session):
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
                "trip_event_ids": [t.id for t in flat],
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
    if (g.state != "tripped" or g.sweep_state not in _INCOMPLETE_SWEEPS
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
            "detail": (f"trip {g.trip_id}'s disarm sweep is still "
                       f"'{g.sweep_state}' {(now.date() - day).days} day(s) later, "
                       f"through every cycle's resume"),
        }})]


def _unset_mandate(
    *, g: GuardrailsState, live_days: dict[date, int]
) -> list[_GuardrailBreach]:
    """RULE 4 (warn): counting live orders while NO mandatory breaker is set.

    The submit-side mandate already refuses real money without all three breakers, so
    this is the conduct-record backstop, not the enforcement: it says the live book
    moved while the brake had nothing configured to catch it. Warn, not alert, because
    the auditor cannot tell a real-money endpoint from a paper-host drill (the broker
    host is env, never DB) and the mandate check is what actually gates real money.
    Evaluated against the breakers AS SET NOW -- an operator who set them afterwards
    leaves no way to reconstruct the day's configuration."""
    if any(v is not None for v in
           (g.max_daily_loss_usd, g.max_trades_per_day, g.max_drawdown_usd)):
        return []
    out: list[_GuardrailBreach] = []
    for day in sorted(live_days):
        n = live_days[day]
        out.append(_GuardrailBreach(
            day=day, key=f"guardrail-unset-mandate:{day.isoformat()}", severity="warn",
            findings={"guardrail_breach": {
                "rule": "unset-mandate", "day": day.isoformat(), "n_live_counting": n,
                "detail": (f"{n} counting live order(s) with no mandatory breaker set "
                           f"(daily loss / trades per day / drawdown all unset)"),
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

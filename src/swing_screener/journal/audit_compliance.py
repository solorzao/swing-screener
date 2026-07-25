"""The System Behavior Auditor's compliance grader -- code owns every number.

Pure aggregation over MACHINE conduct data (``ExecutionLog`` hard-limit sums, the
machine book's realized R via ``repo.realized_r_on``, reject/clamp statuses,
``DisarmEvent``, and the guardrail brake's own event/email trail) for one audited
period. It deliberately imports NO personal-book model: ``Trade.override`` is Coach
data and off-limits here (the Auditor audits the machine, not the human's discipline).

GUARDRAIL CONDUCT (Task 14) follows the ``n_clamps`` precedent exactly: the brake
FIRING is the machine behaving correctly, so every sanctioned brake artifact -- the
submit-side ``guardrail: ...`` clamp, the trip protocol's own sweep ``DisarmEvent``,
the kill-switch and manual-HALT sweeps, the trip events themselves -- is counted as a
FACT here and never graded as a breach. Only a disarm the guardrails machinery did not
author (``n_unexplained_disarms``) is anomalous. The hard conduct FAILURES around the
brake (a live submit on a tripped day, an unmailed trip, a stuck sweep, live orders
with no mandate set) are breach-scan rules in ``audit_run``, not counters here.

Scoped to DB-native signals. Config-churn (proposals approve/withdraw) lives in git
edge files, not the DB, so it is a follow-on for the audit_run CLI (which has git
access), not this pure grader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import AgentGuardrailEvent, DisarmEvent, EmailLog, ExecutionLog
from swing_screener.db.repo import LIMIT_COUNTING_STATUSES, realized_r_on

# ExecutionLog.status values that mean an order did NOT go through cleanly.
_REJECT_STATUSES = frozenset({"rejected", "rejected_live", "skipped"})

#: the ``ExecutionLog.detail`` prefix the submit-side brake stamps on the ``skipped``
#: row it writes when a breaker is breached (``pipeline.execution._guardrail_block``:
#: ``detail = f"guardrail: {brake_reason}"``). Deliberately NOT stamped on the
#: mode-off / unplaceable paths -- that module keeps this prefix reserved for the
#: brake precisely so the Auditor can grep it.
GUARDRAIL_CLAMP_PREFIX = "guardrail: "

#: the ``DisarmEvent.reason`` prefix the trip protocol writes for its own sweep
#: (``pipeline.guardrails._run_sweep``: ``reason=f"guardrail:{breaker}"``).
GUARDRAIL_SWEEP_PREFIX = "guardrail:"
#: the two other SANCTIONED sweep reasons ``pipeline.guardrails.record_disarm_event``
#: writes: the mid-dispatch kill switch and the manual-HALT brake (notify/run.py).
KILLSWITCH_DISARM_REASON = "kill-switch"
HALT_DISARM_REASON = "halt"

#: the ``EmailLog`` kind that is BOOKKEEPING, not a sent email: the live-rejection
#: alert writes one ``execution-cover`` row per alerted ExecutionLog id purely so the
#: at-least-once retry can join on coverage (Task 11), alongside the ONE ``execution``
#: display row for the email itself. Counting them would inflate every "emails sent"
#: conduct number by the size of the rejected batch. Restated as a literal -- this
#: grader is import-light by contract (``db.*`` only; a journal -> notify edge for one
#: string would hand every importer the notify layer) -- exactly as
#: ``cockpit/routers/reference.py`` restates it; tests/journal/test_audit_compliance.py
#: pins it against ``notify.alerts.REJECTION_COVER_KIND`` so the two can never drift.
_BOOKKEEPING_EMAIL_KIND = "execution-cover"

#: the trip-alert ``EmailLog`` kind (``n_guardrail_alerts`` counts these). Same mirror
#: posture and the same anti-drift pin: ``notify.alerts.TRIP_ALERT_KIND`` owns the
#: value, this grader restates it rather than take a journal -> notify import.
_TRIP_ALERT_KIND = "guardrail"


def is_sanctioned_disarm(reason: str) -> bool:
    """True when the guardrails machinery itself authored this ``DisarmEvent``.

    The three sanctioned venue-moving sweeps -- a trip's own sweep
    (``guardrail:<breaker>``), the mid-dispatch kill switch, the manual HALT -- are
    EXPECTED conduct: the operator already heard about them (trip email / the action
    they took), and flagging them would cry wolf on precisely the days the brake
    worked. Anything else is an unexplained disarm and stays a breach (audit_run).
    """
    return (
        reason.startswith(GUARDRAIL_SWEEP_PREFIX)
        or reason in (KILLSWITCH_DISARM_REASON, HALT_DISARM_REASON)
    )


@dataclass(frozen=True)
class ComplianceFindings:
    """Deterministic conduct scorecard for one period. ``reject_rate`` is None when no
    execution rows exist (unmeasurable, not zero). ``n_clamps`` counts "skipped" rows --
    the limit engine doing its job (good conduct, surfaced separately, never a breach)."""

    # [{date, account, notional, risk_dollars, notional_cap, day_r, loss_cap_r}]
    cap_breaches: list[dict[str, object]]
    reject_rate: float | None
    n_rejected: int
    n_clamps: int
    n_total: int
    n_disarms: int
    # --- guardrail conduct: EXPECTED activity, counted like n_clamps (never a breach)
    n_guardrail_clamps: int      # 'skipped' rows the BRAKE wrote (subset of n_clamps)
    n_guardrail_sweeps: int      # DisarmEvents from a trip's own sweep
    n_killswitch_sweeps: int     # DisarmEvents from the mid-dispatch kill switch
    n_halt_sweeps: int           # DisarmEvents from the manual HALT brake
    n_unexplained_disarms: int   # n_disarms minus the three above -- the anomalous ones
    n_guardrail_trips: int       # 'trip' guardrail events in the period
    trip_sources: dict[str, int] # trips by emitter (digest/screen/exitcheck/cockpit)
    n_guardrail_alerts: int      # trip-alert emails LOGGED in the period
    n_emails_sent: int           # emails actually sent (bookkeeping kinds excluded)


def compliance_findings(
    session: Session,
    *,
    period_from: date,
    period_to: date,
    max_daily_notional: float | None,
    max_daily_loss: float | None,
) -> ComplianceFindings:
    """Grade machine execution conduct over ``[period_from, period_to]``. Caps of None
    mean uncapped -> never a breach for that dimension.

    UNIT CONTRACT: ``max_daily_loss`` is the execution adapter's realized-loss circuit
    breaker in **R**, NOT a $ cap on entry risk (see execution.py's PER-DAY-LOSS UNIT
    DECISION: ``max_daily_loss=2.0`` blocks new orders once the account has realized
    -2R or worse today). The auditor grades with the breaker's own source
    (``repo.realized_r_on``) and predicate (``day_r <= -cap``), on the (day, account)
    cells that had counted order activity. ``max_daily_notional`` stays a $ cap on the
    day's summed counted notional."""
    logs = list(session.scalars(
        select(ExecutionLog)
        .where(ExecutionLog.created_date >= period_from,
               ExecutionLog.created_date <= period_to)
        .order_by(ExecutionLog.created_date)
    ))

    # Per-(day, account) hard-limit sums vs the mandate -- counting ONLY the statuses the
    # limit engine counts (repo.LIMIT_COUNTING_STATUSES) and grading per account, exactly
    # as the caps are enforced (execution_logs_for_day). A "skipped" clamp row carries the
    # BLOCKED order's full size; summing it would flag a breach on precisely the days the
    # machine clamped correctly.
    by_key_notional: dict[tuple[date, str], float] = {}
    by_key_risk: dict[tuple[date, str], float] = {}
    for log in logs:
        if log.status not in LIMIT_COUNTING_STATUSES:
            continue
        key = (log.created_date, log.account)
        by_key_notional[key] = by_key_notional.get(key, 0.0) + log.notional
        by_key_risk[key] = by_key_risk.get(key, 0.0) + log.risk_dollars

    cap_breaches: list[dict[str, object]] = []
    for day, account in sorted(by_key_notional):
        notional = by_key_notional[(day, account)]
        risk_dollars = by_key_risk[(day, account)]
        over_notional = max_daily_notional is not None and notional > max_daily_notional
        # the loss cap is graded in the breaker's own unit: the day's summed REALIZED R
        # for the account (realized_r_on -- the execution breaker's source), tripping at
        # day_r <= -cap. Entry risk_dollars is $ committed, not a loss; it stays in the
        # breach dict as information only.
        day_r = (realized_r_on(session, run_date=day, account=account)
                 if max_daily_loss is not None else None)
        over_loss = max_daily_loss is not None and day_r is not None and day_r <= -max_daily_loss
        if over_notional or over_loss:
            cap_breaches.append({
                "date": day.isoformat(), "account": account, "notional": notional,
                "risk_dollars": risk_dollars, "notional_cap": max_daily_notional,
                "day_r": day_r, "loss_cap_r": max_daily_loss,
            })

    # reject_rate stays over ALL rows: it is ABOUT the blocked/dirty attempts
    # (rejected / rejected_live / skipped), which the cap sums above exclude.
    n_total = len(logs)
    n_rejected = sum(1 for log in logs if log.status in _REJECT_STATUSES)
    n_clamps = sum(1 for log in logs if log.status == "skipped")
    # the brake's own half of the clamps: same "surfaced, never a breach" posture, but
    # named, so a week of correct braking reads as such instead of as limit-engine churn.
    n_guardrail_clamps = sum(
        1 for log in logs
        if log.status == "skipped" and (log.detail or "").startswith(GUARDRAIL_CLAMP_PREFIX)
    )
    reject_rate = (n_rejected / n_total) if n_total else None

    # datetime-keyed tables (disarms, guardrail events, emails) are windowed on the
    # SAME [period_from 00:00, period_to 23:59:59.999999] bounds the disarm count has
    # always used -- one day-keying convention across the grader.
    span_from = datetime.combine(period_from, time.min)
    span_to = datetime.combine(period_to, time.max)

    disarm_reasons = list(session.scalars(
        select(DisarmEvent.reason).where(
            DisarmEvent.created_at >= span_from, DisarmEvent.created_at <= span_to,
        )
    ))
    n_guardrail_sweeps = sum(1 for r in disarm_reasons
                             if (r or "").startswith(GUARDRAIL_SWEEP_PREFIX))
    n_killswitch_sweeps = sum(1 for r in disarm_reasons if r == KILLSWITCH_DISARM_REASON)
    n_halt_sweeps = sum(1 for r in disarm_reasons if r == HALT_DISARM_REASON)
    # NOTE (deliberate, Task 11): a SECOND sweep an hour after the first is a RESUMED
    # partial sweep, not a double-sweep -- sweep frequency is counted, never graded.
    n_unexplained_disarms = sum(1 for r in disarm_reasons if not is_sanctioned_disarm(r or ""))

    # trips by emitter: digest / screen / exitcheck / cockpit are ALL legitimate trip
    # owners (whoever wins the repo's rows-affected election), so the source split is a
    # fact for the narrative, never a signal that the wrong process tripped the brake.
    trip_sources: dict[str, int] = {}
    for source in session.scalars(
        select(AgentGuardrailEvent.source).where(
            AgentGuardrailEvent.kind == "trip",
            AgentGuardrailEvent.created_at >= span_from,
            AgentGuardrailEvent.created_at <= span_to,
        )
    ):
        trip_sources[source] = trip_sources.get(source, 0) + 1

    email_kinds = list(session.scalars(
        select(EmailLog.kind).where(EmailLog.sent_at >= span_from, EmailLog.sent_at <= span_to)
    ))
    n_guardrail_alerts = sum(1 for k in email_kinds if k == _TRIP_ALERT_KIND)
    n_emails_sent = sum(1 for k in email_kinds if k != _BOOKKEEPING_EMAIL_KIND)

    return ComplianceFindings(
        cap_breaches=cap_breaches, reject_rate=reject_rate, n_rejected=n_rejected,
        n_clamps=n_clamps, n_total=n_total, n_disarms=len(disarm_reasons),
        n_guardrail_clamps=n_guardrail_clamps, n_guardrail_sweeps=n_guardrail_sweeps,
        n_killswitch_sweeps=n_killswitch_sweeps, n_halt_sweeps=n_halt_sweeps,
        n_unexplained_disarms=n_unexplained_disarms,
        n_guardrail_trips=sum(trip_sources.values()), trip_sources=trip_sources,
        n_guardrail_alerts=n_guardrail_alerts, n_emails_sent=n_emails_sent,
    )

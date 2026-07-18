"""The System Behavior Auditor's compliance grader -- code owns every number.

Pure aggregation over MACHINE conduct data (``ExecutionLog`` hard-limit sums, reject/
clamp statuses, ``DisarmEvent``) for one audited period. It deliberately imports NO
personal-book model: ``Trade.override`` is Coach data and off-limits here (the Auditor
audits the machine, not the human's discipline).

Scoped to DB-native signals. Config-churn (proposals approve/withdraw) lives in git
edge files, not the DB, so it is a follow-on for the audit_run CLI (which has git
access), not this pure grader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import DisarmEvent, ExecutionLog
from swing_screener.db.repo import LIMIT_COUNTING_STATUSES

# ExecutionLog.status values that mean an order did NOT go through cleanly.
_REJECT_STATUSES = frozenset({"rejected", "rejected_live", "skipped"})


@dataclass(frozen=True)
class ComplianceFindings:
    """Deterministic conduct scorecard for one period. ``reject_rate`` is None when no
    execution rows exist (unmeasurable, not zero). ``n_clamps`` counts "skipped" rows --
    the limit engine doing its job (good conduct, surfaced separately, never a breach)."""

    # [{date, account, notional, risk, notional_cap, loss_cap}]
    cap_breaches: list[dict[str, object]]
    reject_rate: float | None
    n_rejected: int
    n_clamps: int
    n_total: int
    n_disarms: int


def compliance_findings(
    session: Session,
    *,
    period_from: date,
    period_to: date,
    max_daily_notional: float | None,
    max_daily_loss: float | None,
) -> ComplianceFindings:
    """Grade machine execution conduct over ``[period_from, period_to]``. Caps of None
    mean uncapped -> never a breach for that dimension."""
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
        notional, risk = by_key_notional[(day, account)], by_key_risk[(day, account)]
        over_notional = max_daily_notional is not None and notional > max_daily_notional
        over_loss = max_daily_loss is not None and risk > max_daily_loss
        if over_notional or over_loss:
            cap_breaches.append({
                "date": day.isoformat(), "account": account, "notional": notional,
                "risk": risk, "notional_cap": max_daily_notional, "loss_cap": max_daily_loss,
            })

    # reject_rate stays over ALL rows: it is ABOUT the blocked/dirty attempts
    # (rejected / rejected_live / skipped), which the cap sums above exclude.
    n_total = len(logs)
    n_rejected = sum(1 for log in logs if log.status in _REJECT_STATUSES)
    n_clamps = sum(1 for log in logs if log.status == "skipped")
    reject_rate = (n_rejected / n_total) if n_total else None

    disarm_ids = list(session.scalars(
        select(DisarmEvent.id).where(
            DisarmEvent.created_at >= datetime.combine(period_from, time.min),
            DisarmEvent.created_at <= datetime.combine(period_to, time.max),
        )
    ))

    return ComplianceFindings(
        cap_breaches=cap_breaches, reject_rate=reject_rate, n_rejected=n_rejected,
        n_clamps=n_clamps, n_total=n_total, n_disarms=len(disarm_ids),
    )

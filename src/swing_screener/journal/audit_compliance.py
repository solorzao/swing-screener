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

# ExecutionLog.status values that mean an order did NOT go through cleanly.
_REJECT_STATUSES = frozenset({"rejected", "rejected_live", "skipped"})


@dataclass(frozen=True)
class ComplianceFindings:
    """Deterministic conduct scorecard for one period. ``reject_rate`` is None when no
    execution rows exist (unmeasurable, not zero)."""

    cap_breaches: list[dict[str, object]]  # [{date, notional, risk, notional_cap, loss_cap}]
    reject_rate: float | None
    n_rejected: int
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

    # Per-day hard-limit sums vs the mandate.
    by_day_notional: dict[date, float] = {}
    by_day_risk: dict[date, float] = {}
    for log in logs:
        by_day_notional[log.created_date] = by_day_notional.get(log.created_date, 0.0) + log.notional
        by_day_risk[log.created_date] = by_day_risk.get(log.created_date, 0.0) + log.risk_dollars

    cap_breaches: list[dict[str, object]] = []
    for day in sorted(by_day_notional):
        notional, risk = by_day_notional[day], by_day_risk[day]
        over_notional = max_daily_notional is not None and notional > max_daily_notional
        over_loss = max_daily_loss is not None and risk > max_daily_loss
        if over_notional or over_loss:
            cap_breaches.append({
                "date": day.isoformat(), "notional": notional, "risk": risk,
                "notional_cap": max_daily_notional, "loss_cap": max_daily_loss,
            })

    n_total = len(logs)
    n_rejected = sum(1 for log in logs if log.status in _REJECT_STATUSES)
    reject_rate = (n_rejected / n_total) if n_total else None

    disarm_ids = list(session.scalars(
        select(DisarmEvent.id).where(
            DisarmEvent.created_at >= datetime.combine(period_from, time.min),
            DisarmEvent.created_at <= datetime.combine(period_to, time.max),
        )
    ))

    return ComplianceFindings(
        cap_breaches=cap_breaches, reject_rate=reject_rate, n_rejected=n_rejected,
        n_total=n_total, n_disarms=len(disarm_ids),
    )

"""Suite-wide LLM spend gathering for the cockpit gate/telemetry.

The safety gate must reflect ALL LLM spend, not just the analyst's -- Journal v2's
Coach reviews and Auditor sweeps also cost tokens. This unions ``est_cost_usd`` across
``AnalystCall`` (keyed by ``created_date``) and ``journal_reviews`` / ``system_audits``
(keyed by ``generated_at``). NULL costs are returned as-is so callers can COUNT the
undercount rather than silently absorb it as zero (the disclosed-undercount contract).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import AnalystCall, JournalReview, SystemAudit


def spend_rows_since(session: Session, since: date) -> list[tuple[date, float | None]]:
    """``(date, est_cost_usd)`` for every LLM-spending row since ``since`` (inclusive),
    across all three tables. ``est_cost_usd`` is None on the deterministic/fallback path."""
    rows: list[tuple[date, float | None]] = [
        (d, c) for d, c in session.execute(
            select(AnalystCall.created_date, AnalystCall.est_cost_usd)
            .where(AnalystCall.created_date >= since)
        ).all()
    ]
    for model in (JournalReview, SystemAudit):
        for gen, cost in session.execute(
            select(model.generated_at, model.est_cost_usd)
            .where(model.generated_at.is_not(None))
        ).all():
            if gen is not None and gen.date() >= since:
                rows.append((gen.date(), cost))
    return rows

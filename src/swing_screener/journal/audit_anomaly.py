"""The System Behavior Auditor's anomaly grader -- machine conduct only.

Pure aggregation over machine telemetry for one period: surfacing drought and
would-surface leaks (``ReversalFunnel``), analyst-calibration drift (reusing the
edge engine's ``analyst_calibration``), and execution integrity (orphan paper
``ExitEvent`` rows). ExitEvents are filtered ``is_paper=True`` -- NEVER by account,
because a manual equity close is written ``is_paper=False, account="research"`` and
must never be swept into the machine audit. Imports no personal-book model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import AnalystCall, ExitEvent, PaperTrade, ReversalFunnel
from swing_screener.pipeline.reflect import analyst_calibration


@dataclass(frozen=True)
class AnomalyFindings:
    """Deterministic anomaly scorecard for one period."""

    drought_days: int          # funnel snapshots with detected>0 but surfaced==0
    would_surface_leaks: int   # sum(actionable - surfaced) where actionable > surfaced
    calibration: dict          # analyst_calibration: by_conviction + nudge_vs_baseline_r
    orphan_exit_events: int    # is_paper exits whose trade_id has no PaperTrade


def anomaly_findings(
    session: Session, *, period_from: date, period_to: date
) -> AnomalyFindings:
    """Grade machine anomalies over ``[period_from, period_to]``."""
    funnels = list(session.scalars(
        select(ReversalFunnel)
        .where(ReversalFunnel.run_date >= period_from, ReversalFunnel.run_date <= period_to)
    ))
    drought_days = sum(1 for f in funnels if f.detected > 0 and f.surfaced == 0)
    would_surface_leaks = sum(
        max(0, f.actionable - f.surfaced) for f in funnels
    )

    calls = list(session.scalars(
        select(AnalystCall)
        .where(AnalystCall.created_date >= period_from, AnalystCall.created_date <= period_to)
    ))
    calibration = analyst_calibration(calls)

    # Integrity: paper exit events (is_paper=True, NEVER filtered by account) whose
    # trade_id points at no PaperTrade -- the max(PaperTrade.id) / id-space hazard.
    paper_ids = set(session.scalars(select(PaperTrade.id)))
    exits = list(session.scalars(
        select(ExitEvent).where(
            ExitEvent.created_date >= period_from,
            ExitEvent.created_date <= period_to,
            # `== True` renders `= 1`; `.is_(True)` renders `IS 1`, which SQL Server
            # rejects (IS is NULL-only). sqlite accepts both, so tests can't catch it.
            ExitEvent.is_paper == True,  # noqa: E712
        )
    ))
    orphan_exit_events = sum(
        1 for e in exits if e.trade_id is not None and e.trade_id not in paper_ids
    )

    return AnomalyFindings(
        drought_days=drought_days,
        would_surface_leaks=would_surface_leaks,
        calibration=calibration,
        orphan_exit_events=orphan_exit_events,
    )

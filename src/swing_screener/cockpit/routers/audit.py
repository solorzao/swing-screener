"""The System Behavior Auditor cockpit surface (machine conduct only).

Reads: the weekly conduct reports and the immediate breach feed. Write (header-guarded,
nonce-bumped): acknowledge a report/breach. Read-only oversight -- there is no action
here that changes config or money; the Auditor reports, the human acts.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.cockpit.common import ActionNonce, _require_cockpit, _utc_iso
from swing_screener.db.models import SystemAudit


def _audit_dict(a: SystemAudit) -> dict[str, object]:
    return {
        "id": a.id,
        "kind": a.kind,
        "period_from": a.period_from.isoformat(),
        "period_to": a.period_to.isoformat(),
        "breach_key": a.breach_key,
        "severity": a.severity,
        "acknowledged": a.acknowledged_by_human,
        "generated_at": _utc_iso(a.generated_at),
        "model": a.model,
        "est_cost_usd": a.est_cost_usd,
        "narrative": a.narrative,
        "findings": json.loads(a.findings_json or "{}"),
    }


def build_audit_router(
    *,
    _session: Callable[[], Iterator[Session]],
    action_nonce: ActionNonce,
) -> APIRouter:
    """Auditor endpoints, closed over the session dep + the post-action wake nonce."""
    router = APIRouter()

    @router.get("/api/audit/reports")
    def reports(session: Session = Depends(_session)) -> list[dict[str, object]]:
        """The weekly conduct reports, newest first."""
        rows = session.scalars(
            select(SystemAudit).where(SystemAudit.kind == "weekly")
            .order_by(SystemAudit.id.desc())
        )
        return [_audit_dict(a) for a in rows]

    @router.get("/api/audit/breaches")
    def breaches(session: Session = Depends(_session)) -> list[dict[str, object]]:
        """The immediate breach feed (cap exceedances, disarms), newest first."""
        rows = session.scalars(
            select(SystemAudit).where(SystemAudit.kind == "breach")
            .order_by(SystemAudit.id.desc())
        )
        return [_audit_dict(a) for a in rows]

    @router.post("/api/audit/{audit_id}/ack", dependencies=[Depends(_require_cockpit)])
    def acknowledge(
        audit_id: int, session: Session = Depends(_session)
    ) -> dict[str, object]:
        row = session.get(SystemAudit, audit_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no audit {audit_id}")
        row.acknowledged_by_human = True
        session.commit()
        action_nonce.bump()
        return _audit_dict(row)

    return router

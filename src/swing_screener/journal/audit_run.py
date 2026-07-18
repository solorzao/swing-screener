"""The System Behavior Auditor worker: a weekly conduct sweep + a daily breach scan.

``audit_run weekly`` grades the period (compliance + anomaly), writes ONE idempotent
``SystemAudit(kind="weekly")`` row, and (when enabled + under budget) drafts the
independent audit narrative. ``audit_run breach`` scans the trailing week and writes a
``SystemAudit(kind="breach")`` row per hard breach (a cap exceeded, a disarm) it hasn't
already recorded -- the "flags on the next run after the breach" path (design SS Cadence).

Read-only oversight: it writes only its OWN audit rows; it never moves money, changes
config, or disarms. Same code-owns-the-numbers firewall (findings_json authoritative).
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import DisarmEvent, SystemAudit
from swing_screener.db.session import get_engine
from swing_screener.journal.audit_anomaly import anomaly_findings
from swing_screener.journal.audit_author import breach_narrative, draft_audit, template_audit
from swing_screener.journal.audit_compliance import compliance_findings
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
    elif anom.drought_days or anom.orphan_exit_events or comp.n_disarms:
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
    the rows written this run."""
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
    # narrative and findings_json state what happened, not a day with no detail)
    disarms_by_day: dict[date, list[DisarmEvent]] = {}
    for event in session.scalars(
        select(DisarmEvent).where(
            DisarmEvent.created_at >= datetime.combine(day_from, time.min),
            DisarmEvent.created_at <= datetime.combine(day_to, time.max))):
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

    if written:
        session.commit()
    return written


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
    sub.add_parser("breach", help="scan the trailing week for hard breaches (caps, disarms)")
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

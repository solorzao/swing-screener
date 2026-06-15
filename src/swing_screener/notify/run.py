"""Digest orchestrator: selection -> Claude analysis -> PDF -> email.

Wires the Phase 4 pieces into one idempotent run. For a given ``(kind,
run_date)`` it selects the picks, has Claude narrate each one (with a
deterministic fallback baked into ``analyze_signal``), builds the attachment
PDF, and sends the digest email plus a standalone exit-alert email when there
are pending exit events.

Idempotency is enforced via an ``EmailLog`` row per ``(kind, run_date)``: a
second run for the same day is a no-op. PDF rendering is best-effort and must
never block the email. The Anthropic client and the SMTP send function are
injectable seams so tests never hit the network or send mail.
"""

import argparse
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swing_screener.config_secrets import get_secret
from swing_screener.data.universe import names_by_ticker
from swing_screener.db.models import EmailLog, ExitEvent, Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import select as sel
from swing_screener.notify.alerts import compose_exit_alert
from swing_screener.notify.analysis import SignalFacts, analyze_signal
from swing_screener.notify.body import AlertLine, DigestPick, compose_digest_body
from swing_screener.notify.pdf import PdfPick, build_digest_pdf
from swing_screener.notify.transport import resolve_sender
from swing_screener.pipeline.exitcheck import ExitCheckResult, LatestBarsFn, run_exit_check
from swing_screener.settings import load_settings

log = logging.getLogger(__name__)

SmtpSend = Callable[..., None]

_PICKERS = {"daily": sel.daily_picks, "weekly": sel.weekly_picks, "monthly": sel.monthly_picks}


@dataclass(frozen=True)
class DigestResult:
    n_picks: int
    pdf_attached: bool
    sent: bool


def _exit_alert_key(alerts: list[ExitEvent]) -> str:
    """Deterministic key over the SET of exit-event ids (sha1, 40 chars).

    Idempotency is keyed on the *set of events*, not the calendar day, so a later
    hour with a NEW exit yields a different key (and thus a new alert) while a
    re-run over the same events maps to the same key (a no-op). Sorting by id
    makes the key order-independent; it distinguishes {1,2} from {1,2,3} from
    {1,3}. The 40-char sha1 hexdigest fits ``EmailLog.alert_key`` (String(64)).
    """
    ids = ",".join(str(a.id) for a in sorted(alerts, key=lambda a: a.id))
    return hashlib.sha1(ids.encode()).hexdigest()


def _exit_already_sent(session: Session, run_date: date, alert_key: str) -> bool:
    """True if an exit alert for this exact event set already logged on this date."""
    stmt = select(EmailLog).where(
        EmailLog.kind == "exit",
        EmailLog.run_date == run_date,
        EmailLog.alert_key == alert_key,
    )
    return session.scalars(stmt).first() is not None


def _emit_pending_exit_alert(session: Session, run_date: date, recipient: str,
                             smtp_send: SmtpSend) -> bool:
    """Send a standalone exit-alert email if real exit events are pending today.

    Exit alerts are urgent and tracked independently of the digest, keyed on the
    SET of pending exit events (see ``_exit_alert_key``) rather than the calendar
    day. That makes the hourly cadence work: a NEW exit firing later in the
    session produces a fresh key and a new alert, while a re-run over the same
    events is a no-op. Returns True iff an email was sent. Shared by
    ``send_digest`` and the ``exit`` run path.

    Ordering is deliberate: we SEND then LOG (not log-then-send). An exit alert
    can be an urgent hard stop, so we prioritize never LOSING it over strictly
    preventing a rare duplicate -- if the SMTP send fails we leave no log row, so
    the next hourly run retries. The unique constraint plus the
    ``_exit_already_sent`` pre-check make the common sequential re-run a clean
    no-op; the ``IntegrityError`` catch only guards the rare concurrent-replica
    race (which may double-send -- accepted).
    """
    alerts = sel.pending_exit_alerts(session, run_date)
    if not alerts:
        return False
    key = _exit_alert_key(alerts)
    if _exit_already_sent(session, run_date, key):
        return False
    alert_email = compose_exit_alert(alerts, run_date)
    smtp_send(to=recipient, subject=alert_email.subject, text=alert_email.text,
              html=alert_email.html, attachments=[])  # SEND FIRST (see docstring)
    session.add(EmailLog(sent_at=datetime.now(UTC), kind="exit",
                         subject=alert_email.subject, run_date=run_date, alert_key=key))
    try:
        session.commit()
    except IntegrityError:  # lost the concurrent-replica race; the row already exists
        session.rollback()
    return True


def _facts(sig: Signal) -> SignalFacts:
    return SignalFacts(
        ticker=sig.ticker, timeframe=sig.timeframe, trade_type=sig.horizon, score=sig.score,
        mtf_aligned=sig.mtf_aligned, quality_tier=sig.quality_tier,
        volatility_tier=sig.volatility_tier, oversold=sig.oversold,
        trigger_close=sig.trigger_close, atr=sig.atr, rsi=sig.rsi, entry_floor=sig.entry_floor,
        entry_ceiling=sig.entry_ceiling, stop=sig.stop, target=sig.target,
    )


def _already_sent(session: Session, kind: str, run_date: date) -> bool:
    stmt = select(EmailLog).where(EmailLog.kind == kind, EmailLog.run_date == run_date)
    return session.scalars(stmt).first() is not None


def send_digest(*, kind: str, db_url: str, run_date: date | None = None, to: str | None = None,
                pdf_dir: Path = Path(".digests"), anthropic_client: object | None = None,
                smtp_send: SmtpSend | None = None) -> DigestResult:
    send = smtp_send or resolve_sender()  # env-driven transport (ACS or SMTP)
    run_date = run_date or date.today()
    recipient = to or get_secret("DIGEST_TO")
    if not recipient:  # fail fast, before any billable Claude calls
        raise RuntimeError("no recipient: set DIGEST_TO or pass to=")
    engine = get_engine(db_url)
    with Session(engine) as session:
        alerts = sel.pending_exit_alerts(session, run_date)
        _emit_pending_exit_alert(session, run_date, recipient, send)

        picks = _PICKERS[kind](session, run_date)
        if _already_sent(session, kind, run_date):  # don't re-send the same digest
            return DigestResult(n_picks=len(picks), pdf_attached=False, sent=False)

        names = names_by_ticker()  # ticker -> company name, loaded once
        digest_picks: list[DigestPick] = []
        pdf_picks: list[PdfPick] = []
        for sig in picks:
            facts = _facts(sig)
            analysis = analyze_signal(facts, client=anthropic_client)  # type: ignore[arg-type]
            name = names.get(sig.ticker, "")
            digest_picks.append(DigestPick(sig.ticker, name, sig.horizon, analysis.core_reason))
            pdf_picks.append(PdfPick(
                ticker=sig.ticker, name=name, trade_type=sig.horizon, score=sig.score,
                chart_path=sig.chart_path, entry_floor=sig.entry_floor,
                entry_ceiling=sig.entry_ceiling, stop=sig.stop, target=sig.target,
                risk_reward=facts.risk_reward, quality_tier=sig.quality_tier,
                volatility_tier=sig.volatility_tier, oversold=sig.oversold,
                mtf_aligned=sig.mtf_aligned, atr_pct=facts.atr_pct,
                rationale=analysis.rationale,
            ))

        pdf_path: Path | None = None
        if digest_picks:
            try:
                pdf_path = build_digest_pdf(
                    pdf_picks, Path(pdf_dir) / f"{kind}_{run_date:%Y%m%d}.pdf"
                )
            except Exception:  # PDF must never block the email
                log.warning("PDF build failed for %s %s", kind, run_date, exc_info=True)
                pdf_path = None
        pdf_attached = pdf_path is not None

        alert_lines = [
            AlertLine(ticker=(a.message.split(" ", 1)[0] if a.message else ""),
                      tier=a.tier, reason=a.reason, message=a.message)
            for a in alerts
        ]
        body = compose_digest_body(kind, run_date, digest_picks, alert_lines, has_pdf=pdf_attached)
        send(to=recipient, subject=body.subject, text=body.text, html=body.html,
             attachments=([pdf_path] if pdf_path is not None else []))

        session.add(EmailLog(sent_at=datetime.now(UTC), kind=kind, subject=body.subject,
                             run_date=run_date))
        session.commit()
        return DigestResult(n_picks=len(picks), pdf_attached=pdf_attached, sent=True)


def run_exit_check_and_alert(*, db_url: str, run_date: date | None = None, to: str | None = None,
                             smtp_send: SmtpSend | None = None,
                             latest_bars_fn: LatestBarsFn | None = None) -> ExitCheckResult:
    """Intraday exit path: PRODUCE today's real exit events, then SEND the alert.

    Unlike the digest kinds there is no ``_PICKERS["exit"]``; this is a thin
    two-step path. (a) ``run_exit_check`` records ``is_paper=False`` ExitEvents
    for any open real trade whose latest bar trips an exit, then (b) the shared
    ``_emit_pending_exit_alert`` helper emails them (subject "Exit", no PDF),
    deduped per exit-event-SET so a later hour with a NEW exit still alerts.
    """
    send = smtp_send or resolve_sender()  # env-driven transport (ACS or SMTP)
    run_date = run_date or date.today()
    recipient = to or get_secret("DIGEST_TO")
    if not recipient:
        raise RuntimeError("no recipient: set DIGEST_TO or pass to=")

    kwargs: dict[str, object] = {"db_url": db_url, "today": run_date}
    if latest_bars_fn is not None:
        kwargs["latest_bars_fn"] = latest_bars_fn
    result = run_exit_check(**kwargs)  # type: ignore[arg-type]

    with Session(get_engine(db_url)) as session:
        _emit_pending_exit_alert(session, run_date, recipient, send)
    return result


def main() -> None:
    settings = load_settings()  # absolute paths + env-resolved DB URL (container-safe)
    parser = argparse.ArgumentParser(description="Send a swing-screener email digest.")
    parser.add_argument("--kind", choices=["daily", "weekly", "monthly", "exit"], default="daily")
    parser.add_argument("--db", default=settings.db_url)
    parser.add_argument("--pdf-dir", type=Path, default=settings.pdf_dir)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.kind == "exit":
        exit_result = run_exit_check_and_alert(db_url=args.db)
        log.info("exit check: open=%d exited=%d", exit_result.n_open, exit_result.n_exited)
        return
    result = send_digest(kind=args.kind, db_url=args.db, pdf_dir=args.pdf_dir)
    log.info("digest %s: picks=%d pdf=%s sent=%s",
             args.kind, result.n_picks, result.pdf_attached, result.sent)


if __name__ == "__main__":
    main()

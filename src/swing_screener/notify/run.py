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
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swing_screener.config_secrets import get_secret
from swing_screener.data.universe import names_by_ticker
from swing_screener.db import repo
from swing_screener.db.models import EmailLog, ExitEvent, Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import market_context
from swing_screener.notify import select as sel
from swing_screener.notify.alerts import compose_exit_alert
from swing_screener.notify.analysis import (
    SignalAnalysis,
    SignalFacts,
    analyze_signal,
    analyze_signal_deep,
)
from swing_screener.notify.body import AlertLine, DigestPick, compose_digest_body
from swing_screener.notify.pdf import PdfPick, build_digest_pdf
from swing_screener.notify.transport import resolve_sender
from swing_screener.pipeline.exitcheck import ExitCheckResult, LatestBarsFn, run_exit_check
from swing_screener.settings import load_settings
from swing_screener.storage.blob import blob_enabled, download_bytes

log = logging.getLogger(__name__)

SmtpSend = Callable[..., None]

_PICKERS = {"daily": sel.daily_picks, "weekly": sel.weekly_picks, "monthly": sel.monthly_picks}


@dataclass(frozen=True)
class DigestResult:
    n_picks: int
    pdf_attached: bool
    sent: bool
    n_reversals: int = 0  # reversal-play picks included (daily digest)


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


def _load_chart_bytes(chart_path: str | None) -> bytes | None:
    """Read a chart PNG's bytes for the deep-analysis image input.

    Mirrors pdf.py: in Azure chart_path is a blob KEY (fetch by key); locally it's
    a filesystem path. Best-effort -- any failure (missing blob/file) yields None,
    and the deep path runs chartless rather than crashing.
    """
    if not chart_path:
        return None
    try:
        if blob_enabled():
            return download_bytes(chart_path)
        p = Path(chart_path)
        return p.read_bytes() if p.exists() else None
    except Exception:  # noqa: BLE001 -- never let a missing chart block the digest
        log.warning("chart load failed for %s; deep analysis runs chartless", chart_path)
        return None


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
                smtp_send: SmtpSend | None = None, force: bool = False,
                deep_analyze_fn: Callable[..., SignalAnalysis] | None = None,
                chart_bytes_loader: Callable[[str | None], bytes | None] | None = None,
                fundamentals_fn: Callable[[str], market_context.Fundamentals] | None = None,
                news_fn: Callable[[str], list[market_context.NewsItem]] | None = None,
                ) -> DigestResult:
    send = smtp_send or resolve_sender()  # env-driven transport (ACS or SMTP)
    recipient = to or get_secret("DIGEST_TO")
    if not recipient:  # fail fast, before any billable Claude calls
        raise RuntimeError("no recipient: set DIGEST_TO or pass to=")
    # Deep-analysis seams (default to the real impls; tests inject fakes). Whether
    # the deep path actually runs is gated by settings below, NOT by these.
    cfg = load_settings()
    deep_analyze = deep_analyze_fn or analyze_signal_deep
    load_chart = chart_bytes_loader or _load_chart_bytes
    get_fundamentals = fundamentals_fn or market_context.get_fundamentals
    get_news = news_fn or market_context.get_recent_news
    deep_on = cfg.deep_analysis_enabled and kind in cfg.deep_analysis_kinds

    engine = get_engine(db_url)
    with Session(engine) as session:
        # The digest summarizes the LATEST screen run -- the morning digest reflects
        # the prior evening's screen (they run on different days), so defaulting to
        # date.today() would query a run_date with no signals. An explicit run_date
        # (tests / backfill) overrides.
        if run_date is None:
            run_date = repo.latest_run_date(session) or date.today()
        alerts = sel.pending_exit_alerts(session, run_date)
        _emit_pending_exit_alert(session, run_date, recipient, send)

        picks = _PICKERS[kind](session, run_date)
        already = _already_sent(session, kind, run_date)
        if already and not force:  # don't re-send the same digest (force overrides for ad-hoc resends)
            return DigestResult(n_picks=len(picks), pdf_attached=False, sent=False)

        names = names_by_ticker()  # ticker -> company name, loaded once

        def _build_picks(sigs: list[Signal]) -> tuple[list[DigestPick], list[PdfPick]]:
            """Build the (DigestPick, PdfPick) lists for a set of signals, running the
            Opus deep analyst on the top-N when enabled."""
            dps: list[DigestPick] = []
            pps: list[PdfPick] = []
            for i, sig in enumerate(sigs):
                facts = _facts(sig)
                if deep_on and i < cfg.deep_analysis_top_n:
                    # Opus analyst: chart image + fundamentals/news + web-searched sentiment.
                    context_text = market_context.context_block(
                        get_fundamentals(sig.ticker), get_news(sig.ticker))
                    analysis = deep_analyze(
                        facts, chart_bytes=load_chart(sig.chart_path), context_text=context_text,
                        client=anthropic_client,  # type: ignore[arg-type]  # test seam may be a fake
                        model=cfg.analysis_model, reasoning=cfg.analysis_reasoning,
                        max_searches=cfg.analysis_max_searches)
                else:
                    analysis = analyze_signal(facts, client=anthropic_client)  # type: ignore[arg-type]
                name = names.get(sig.ticker, "")
                dps.append(DigestPick(sig.ticker, name, sig.horizon, analysis.core_reason,
                                      score=sig.score, strength=sig.strength,
                                      is_deep=analysis.is_deep))
                pps.append(PdfPick(
                    ticker=sig.ticker, name=name, trade_type=sig.horizon, score=sig.score,
                    chart_path=sig.chart_path, entry_floor=sig.entry_floor,
                    entry_ceiling=sig.entry_ceiling, stop=sig.stop, target=sig.target,
                    risk_reward=facts.risk_reward, quality_tier=sig.quality_tier,
                    volatility_tier=sig.volatility_tier, oversold=sig.oversold,
                    mtf_aligned=sig.mtf_aligned, atr_pct=facts.atr_pct,
                    rationale=analysis.rationale, is_deep=analysis.is_deep, strength=sig.strength))
            return dps, pps

        digest_picks, pdf_picks = _build_picks(picks)
        # Reversal "Top 5" -- daily digest only for now (weekly/monthly stay continuation).
        reversal_digest: list[DigestPick] | None = None
        reversal_pdf: list[PdfPick] = []
        if kind == "daily":
            reversal_digest, reversal_pdf = _build_picks(sel.reversal_picks(session, run_date))

        pdf_path: Path | None = None
        if digest_picks or reversal_pdf:
            try:
                pdf_path = build_digest_pdf(
                    pdf_picks, Path(pdf_dir) / f"{kind}_{run_date:%Y%m%d}.pdf",
                    reversal_picks=reversal_pdf or None,
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
        body = compose_digest_body(kind, run_date, digest_picks, alert_lines,
                                   has_pdf=pdf_attached, reversal_picks=reversal_digest)
        send(to=recipient, subject=body.subject, text=body.text, html=body.html,
             attachments=([pdf_path] if pdf_path is not None else []))

        if not already:  # a forced resend reuses the existing day marker (no duplicate row)
            session.add(EmailLog(sent_at=datetime.now(UTC), kind=kind, subject=body.subject,
                                 run_date=run_date))
            session.commit()
        return DigestResult(n_picks=len(picks), pdf_attached=pdf_attached, sent=True,
                            n_reversals=len(reversal_digest or []))


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
    parser.add_argument("--force", action="store_true",
                        help="resend even if a digest for this (kind, day) already went out")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.kind == "exit":
        exit_result = run_exit_check_and_alert(db_url=args.db)
        log.info("exit check: open=%d exited=%d", exit_result.n_open, exit_result.n_exited)
        return
    # SWING_FORCE_RESEND lets a scheduled container force a resend without changing
    # its args -- toggle the env, run once, untoggle (used for ad-hoc verification).
    force = args.force or os.environ.get("SWING_FORCE_RESEND", "").lower() in {"1", "true", "yes"}
    result = send_digest(kind=args.kind, db_url=args.db, pdf_dir=args.pdf_dir, force=force)
    log.info("digest %s: picks=%d reversals=%d pdf=%s sent=%s",
             args.kind, result.n_picks, result.n_reversals, result.pdf_attached, result.sent)


if __name__ == "__main__":
    main()

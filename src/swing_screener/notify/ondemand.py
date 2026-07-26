"""On-demand single-ticker deep-analysis worker + CLI.

Drains the ``analysis_requests`` queue: for each claimed request, fetch the four
timeframes, build the deterministic per-timeframe reads, render a chart for every
timeframe that has a firing setup, run ONE Opus multi-timeframe analyst pass
(always deep -- there is no ``deep_analysis_enabled`` gate here; the user asked
for it), build a PDF, and email it. Charts and the PDF are uploaded to the
private blob store when one is configured (so they survive the container), else
they stay on the local filesystem.

Per-request isolation: :func:`process_one` NEVER raises -- any failure flips the
request to ``failed`` with the error, so one bad ticker never aborts the batch.
The email is idempotent via an ``EmailLog(kind="ondemand", alert_key=str(id))``
row, mirroring the digest dedup in ``notify.run``.
"""

import argparse
import dataclasses
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.config_secrets import get_secret
from swing_screener.data.universe import names_by_ticker
from swing_screener.db import repo
from swing_screener.db.models import EmailLog
from swing_screener.db.session import get_engine
from swing_screener.notify.analysis import analyze_ticker_deep
from swing_screener.notify.body import compose_ticker_report_body
from swing_screener.notify.pdf import build_ticker_report_pdf
from swing_screener.notify.ticker_report import (
    TickerReport,
    TimeframeRead,
    build_ticker_reads,
)
from swing_screener.notify.transport import resolve_sender
from swing_screener.pipeline.analyze import build_frames
from swing_screener.pipeline.run import _fetch_all_timeframes, _migrate_with_retry, _resolve_db_url
from swing_screener.settings import Settings, load_settings
from swing_screener.storage.blob import blob_enabled, upload_bytes, upload_chart

log = logging.getLogger(__name__)

# Rows stuck 'running' longer than this are presumed orphaned (the worker that
# claimed them crashed) and get requeued for re-processing on the next pass.
_STALE_AFTER = timedelta(minutes=30)


def _ondemand_already_sent(session: Session, request) -> bool:
    """True if this request's on-demand email already logged (idempotency check).

    Keyed on the REQUEST's own date (``requested_at.date()``), never the worker's
    ``today``: a row that crashed after its email and got requeued may retry past
    midnight, and a today-keyed check would miss yesterday's log row and email
    twice (2026-07-17 audit M4b). The write below stamps the same date.
    """
    stmt = select(EmailLog).where(
        EmailLog.kind == "ondemand",
        EmailLog.run_date == request.requested_at.date(),
        EmailLog.alert_key == str(request.id),
    )
    return session.scalars(stmt).first() is not None


def _render_reads_with_charts(
    ticker: str, reads: list[TimeframeRead], *, settings: Settings, today: date
) -> tuple[list[TimeframeRead], list[str], list[bytes]]:
    """Render a chart per timeframe that HAS a setup; return (reads, keys, image bytes).

    Reads without a firing setup keep ``chart_path=None`` (nothing to chart). For
    each firing read we render the PNG locally, upload it to the blob store when
    one is configured (``chart_path`` becomes the blob KEY), else keep the local
    path. The raw PNG bytes feed the Opus analyst's vision input regardless.
    """
    # Local import: charts.render pulls matplotlib, which is heavy -- defer it so
    # importing this module (e.g. for the CLI help) stays cheap.
    from swing_screener.charts.render import render_chart

    chart_dir = Path(settings.chart_dir)
    chart_dir.mkdir(parents=True, exist_ok=True)
    out_reads: list[TimeframeRead] = []
    chart_keys: list[str] = []
    chart_bytes: list[bytes] = []
    for read in reads:
        if read.setup is None:
            out_reads.append(read)
            continue
        basename = f"{ticker}_{read.timeframe}_{today:%Y%m%d}.png"
        path = chart_dir / basename
        render_chart(read.setup.frame, read.setup.ctx, read.setup.zone, path)
        chart_bytes.append(path.read_bytes())
        if blob_enabled():
            key = f"{today:%Y%m%d}/{basename}"
            upload_chart(path, key)
            chart_path = key
        else:
            chart_path = str(path)
        chart_keys.append(chart_path)
        out_reads.append(dataclasses.replace(read, chart_path=chart_path))
    return out_reads, chart_keys, chart_bytes


def process_one(session, request, *, settings, cfg, today, now,
                client=None, sender=None) -> None:
    """Run ONE request end to end; NEVER raises (fail_analysis_request on any exception)."""
    try:
        ticker = request.ticker
        bars_by_tf = _fetch_all_timeframes(
            ticker, cache_dir=settings.cache_dir, today=today, cfg=cfg)
        if not bars_by_tf:
            repo.fail_analysis_request(
                session, request.id, error=f"no data for {ticker}", finished_at=now)
            return

        frames = build_frames(bars_by_tf, cfg)
        reads = build_ticker_reads(ticker, frames, cfg)
        reads, chart_keys, chart_bytes = _render_reads_with_charts(
            ticker, reads, settings=settings, today=today)

        name = names_by_ticker().get(ticker, "")
        report0 = TickerReport(
            ticker=ticker, name=name, run_at=now, reads=reads,
            summary="", analysis_text="", is_deep=False)
        analysis = analyze_ticker_deep(
            report0, charts=chart_bytes, client=client, model=settings.analysis_model,
            reasoning=settings.analysis_reasoning,
            max_searches=settings.analysis_max_searches, web_search=True)
        report = dataclasses.replace(
            report0, summary=analysis.summary, analysis_text=analysis.analysis_text,
            is_deep=analysis.is_deep)

        pdf_dir = Path(settings.pdf_dir)
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = build_ticker_report_pdf(
            report, pdf_dir / f"{ticker}_{today:%Y%m%d}.pdf")
        if blob_enabled():
            pdf_key = upload_bytes(f"{today:%Y%m%d}/{pdf_path.name}", pdf_path.read_bytes())
        else:
            pdf_key = str(pdf_path)

        recipient = request.recipient or get_secret("DIGEST_TO")
        if recipient:
            # Idempotent send: one email per request, deduped on (request date, id)
            # -- the request's OWN date, so a cross-midnight retry still matches the
            # existing log row (mirrors notify.run's EmailLog guard otherwise).
            if not _ondemand_already_sent(session, request):
                content = compose_ticker_report_body(report)
                send = sender or resolve_sender()  # env-driven transport (ACS or SMTP)
                send(to=recipient, subject=content.subject, text=content.text,
                     html=content.html, attachments=[pdf_path])
                session.add(EmailLog(
                    sent_at=datetime.now(UTC), kind="ondemand", subject=content.subject,
                    run_date=request.requested_at.date(), alert_key=str(request.id)))
                session.commit()
        else:
            log.warning("no recipient for request %s; completing without email", request.id)

        repo.complete_analysis_request(
            session, request.id, summary=analysis.summary, pdf_blob_key=pdf_key,
            chart_blob_keys=",".join(chart_keys), finished_at=now,
            # The billed spend (approximate list price) -- this path has NO cap, so
            # the persisted estimate is its only cost visibility. None = no billed
            # call captured (deterministic fallback), an honest unknown not a $0.
            est_cost_usd=analysis.usage.est_cost_usd if analysis.usage else None)
    except Exception as exc:
        # Leak posture: the stored error reaches the cockpit wire (/api/analysis and
        # the Zone E ticker), and fetch/SDK messages can embed hosts, URLs, and keys
        # -- persist the exception CLASS only (the broker_error_detail convention);
        # the full traceback goes to the LOG for the operator. The curated
        # "no data for {ticker}" branch above stays verbatim (safe by construction).
        log.exception("on-demand request %s failed", request.id)
        session.rollback()
        repo.fail_analysis_request(
            session, request.id, error=f"error ({type(exc).__name__})", finished_at=now)


def process_pending(session, *, settings, now, today=None, cfg=None,
                    client=None, sender=None, limit=10) -> int:
    """claim_queued_requests then process_one each; return count processed."""
    cfg = cfg or StrategyConfig()
    today = today or now.date()
    # Crash recovery: requeue rows a dead replica left stuck 'running' before we claim,
    # so the retry re-processes them (claim only picks up 'queued').
    repo.requeue_stale_running(session, cutoff=now - _STALE_AFTER)
    claimed = repo.claim_queued_requests(session, now=now, limit=limit)
    for request in claimed:
        process_one(session, request, settings=settings, cfg=cfg, today=today,
                    now=now, client=client, sender=sender)
    return len(claimed)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process queued on-demand single-ticker analysis requests.")
    settings = load_settings()
    parser.add_argument("--db", default=None)
    parser.add_argument("--cache-dir", type=Path, default=settings.cache_dir)
    parser.add_argument("--chart-dir", type=Path, default=settings.chart_dir)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    db_url = _resolve_db_url(args.db)
    if db_url.startswith("mssql"):  # Azure SQL: Alembic owns the schema, upgrade first
        _migrate_with_retry(db_url)
    # Honor CLI dir overrides via env so load_settings (read inside the worker) sees them.
    os.environ["SWING_CACHE_DIR"] = str(args.cache_dir)
    os.environ["SWING_CHART_DIR"] = str(args.chart_dir)
    settings = load_settings()

    engine = get_engine(db_url)
    with Session(engine) as session:
        n = process_pending(
            session, settings=settings, now=datetime.now(UTC), limit=args.limit)
    log.info("on-demand processed=%d", n)


if __name__ == "__main__":
    main()

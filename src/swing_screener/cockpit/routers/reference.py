"""The Reference screen's log reads (exit log, universe, digest log) and the
Zone E event ticker -- one router because the ticker is a merged reverse-chron
read over the same log tables the Reference screen lists individually.

Everything here is a GET over the shared session seam: bounded SELECTs, no
broker, no quotes, no resolver. Timestamps leave as unambiguous UTC via
``common._utc_iso``; DATE-only sources anchor at the heartbeats' end-of-day
(``_eod_utc``) so a date can never read stale before it is over.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from swing_screener.cockpit.common import _stored_error_detail, _utc_iso
from swing_screener.db.models import (
    AnalysisRequest,
    AnalystCall,
    EmailLog,
    ExecutionLog,
    ExitEvent,
)
from swing_screener.db.repo import list_universe

#: EmailLog kinds that are BOOKKEEPING, not sent emails: the live-rejection
#: alert writes one ``execution-cover`` row per alerted ExecutionLog id purely so
#: the at-least-once retry can join on coverage (``notify.alerts``), alongside
#: the ONE ``execution`` display row for the email itself. Rendering the
#: coverage rows would show N identical entries per email in both surfaces
#: below. Excluded here rather than filtered client-side so the LIMIT counts
#: real emails. Restated as a literal (no cockpit -> notify import for one
#: string); tests/cockpit/test_reference.py pins it against
#: ``notify.alerts.REJECTION_COVER_KIND`` so the two can never drift.
_BOOKKEEPING_EMAIL_KINDS = ("execution-cover",)


def build_reference_router(
    *,
    _session: Callable[[], Iterator[Session]],
) -> APIRouter:
    """The reference/log endpoints, closed over the app's session seam."""
    router = APIRouter()

    @router.get("/api/ticker")
    def ticker(
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """Zone E's event ticker: the five activity sources merged newest-first.

        Sources: ExitEvent + ExecutionLog + EmailLog + AnalystCall +
        AnalysisRequest. The design doc named "reflections" as a source, but no
        reflections store exists (reflection's artifacts are edge/ files and PRs)
        -- AnalysisRequest is SUBSTITUTED per the plan's scope decision 16; a
        synthetic reflection event can anchor on the verdicts-file mtime later.

        Each source contributes its newest ``limit`` rows (``ORDER BY id DESC
        LIMIT n`` -- id order is the honest recency for DATE-only tables), merged
        in Python and trimmed to ``limit``. DATE-only stamps (exit / execution /
        analyst) anchor at end-of-day UTC (the heartbeats' ``_eod_utc`` rule);
        an AnalysisRequest rides its most recent lifecycle stamp
        (finished > started > requested), so a finishing report resurfaces as the
        event it is -- and its sub-select orders by that SAME coalesced stamp, so
        an old request finishing today can't be starved out of the candidate
        window by ``limit`` newer ids. Ordering is (ts, source, id) descending --
        deterministic; same-instant rows group by source then newest id.

        Row: ``{source, ts, ticker|null, headline, detail}``; exit rows
        ADDITIONALLY carry ``account`` / ``is_paper`` / ``reason`` / ``tier``
        (the exit log's facet axes -- Book=is_paper and Account are DIFFERENT
        axes, see ``exits``). ExitEvent has no ticker column, so exit rows ride
        ``ticker: null`` with the message as the headline.
        """
        entries: list[tuple[datetime, str, int, dict[str, object]]] = []
        for e in session.scalars(select(ExitEvent)
                                 .order_by(ExitEvent.id.desc()).limit(limit)):
            ts = _day_ts(e.created_date)
            entries.append((ts, "exit", e.id, {
                "source": "exit", "ts": ts.isoformat(), "ticker": None,
                "headline": e.message or e.reason, "detail": e.reason,
                "account": e.account, "is_paper": e.is_paper,
                "reason": e.reason, "tier": e.tier,
            }))
        for x in session.scalars(select(ExecutionLog)
                                 .order_by(ExecutionLog.id.desc()).limit(limit)):
            ts = _day_ts(x.created_date)
            # Historical-row shim: rows written before the write-time leak fix
            # carry `broker error: <raw message>` (which can embed the venue host)
            # -- serve the bare label; post-fix rows are class-name-only already.
            detail = ("broker error" if x.detail.startswith("broker error:")
                      else x.detail)
            entries.append((ts, "execution", x.id, {
                "source": "execution", "ts": ts.isoformat(), "ticker": x.ticker,
                "headline": f"{x.side} {x.shares} {x.ticker} "
                            f"@ {x.limit_price:g} ({x.status})",
                "detail": detail,
            }))
        for m in session.scalars(select(EmailLog)
                                 .where(EmailLog.kind.not_in(_BOOKKEEPING_EMAIL_KINDS))
                                 .order_by(EmailLog.id.desc()).limit(limit)):
            ts = _aware(m.sent_at)
            entries.append((ts, "email", m.id, {
                "source": "email", "ts": ts.isoformat(), "ticker": None,
                "headline": m.subject or m.kind, "detail": m.kind,
            }))
        for c in session.scalars(select(AnalystCall)
                                 .order_by(AnalystCall.id.desc()).limit(limit)):
            ts = _day_ts(c.created_date)
            entries.append((ts, "analyst", c.id, {
                "source": "analyst", "ts": ts.isoformat(), "ticker": c.ticker,
                "headline": f"{c.final_conviction} conviction on {c.ticker}",
                "detail": c.nudge_reason,
            }))
        # Ordered by the SAME lifecycle stamp the merge sorts on -- id order would
        # let >limit newer requests starve an old one that just finished out of
        # the candidate window entirely.
        analysis_recency = func.coalesce(
            AnalysisRequest.finished_at, AnalysisRequest.started_at,
            AnalysisRequest.requested_at)
        for r in session.scalars(
                select(AnalysisRequest)
                .order_by(analysis_recency.desc(), AnalysisRequest.id.desc())
                .limit(limit)):
            ts = _aware(r.finished_at or r.started_at or r.requested_at)
            entries.append((ts, "analysis", r.id, {
                "source": "analysis", "ts": ts.isoformat(), "ticker": r.ticker,
                "headline": f"deep analysis {r.status}: {r.ticker}",
                # error text is whitelist-gated (legacy rows hold raw str(exc))
                "detail": r.summary or (_stored_error_detail(r.error) or ""),
            }))
        entries.sort(key=lambda e: (e[0], e[1], e[2]), reverse=True)
        return {"events": [row for _, _, _, row in entries[:limit]]}

    @router.get("/api/exits")
    def exits(
        reason: str | None = Query(default=None, max_length=32),
        book: Literal["paper", "real"] | None = Query(default=None),
        account: str | None = Query(default=None, max_length=16),
        limit: int = Query(default=200, ge=1, le=1000),
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """The Exit Log page as a filterable read -- the retired Streamlit page's
        three facets, server-side (the Task 21 deletion gate's surface).

        The facets are DIFFERENT AXES, deliberately: ``book`` maps to
        ``is_paper`` (paper=True / real=False) while ``account`` is the display
        facet mirroring ``PaperTrade.account`` -- the research grid AND the
        curated intent book both record under ``is_paper=True``, so the Book
        facet alone can't separate them. ``reason`` is an exact match (the page's
        multiselect sends one at a time). Filters compose (AND); absent = all.
        Newest first (created_date is DATE-only, so id breaks same-day ties);
        boolean comparison stays ``==`` -- ``.is_(False)`` renders ``IS 0``,
        a syntax error on SQL Server (the ``pending_exit_alerts`` precedent).
        """
        stmt = select(ExitEvent)
        if reason is not None:
            stmt = stmt.where(ExitEvent.reason == reason)
        if book is not None:
            stmt = stmt.where(ExitEvent.is_paper == (book == "paper"))
        if account is not None:
            stmt = stmt.where(ExitEvent.account == account)
        stmt = stmt.order_by(ExitEvent.created_date.desc(),
                             ExitEvent.id.desc()).limit(limit)
        return {"exits": [{
            "id": e.id,
            "date": e.created_date.isoformat(),
            "trade_id": e.trade_id,
            "is_paper": e.is_paper,
            "account": e.account,
            "tier": e.tier,
            "reason": e.reason,
            "message": e.message,
        } for e in session.scalars(stmt)]}

    @router.get("/api/universe")
    def universe(
        search: str | None = Query(default=None, max_length=32),
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """The screening universe, ticker-ordered. ``search`` is the repo's
        ticker-LIKE (uppercased, wildcard-escaped -- ``list_universe`` owns the
        escaping; this endpoint adds nothing, so dashboard parity is by
        construction). ``sector`` rides the wire now: the retired page never
        displayed it, but the cockpit's Reference screen does."""
        return {"rows": [{
            "ticker": u.ticker,
            "name": u.name,
            "exchange": u.exchange,
            "market_cap": u.market_cap,
            "avg_dollar_volume": u.avg_dollar_volume,
            "sector": u.sector,
        } for u in list_universe(session, search)]}

    @router.get("/api/emails")
    def emails(
        limit: int = Query(default=100, ge=1, le=1000),
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """The digest log, newest sent first. ``repo.list_email_log`` is
        UNBOUNDED (the retired page ate the whole table), so this endpoint runs
        its own LIMITed SELECT -- default 100 -- rather than slicing a full
        load. ``sent_at`` is served as UTC (``_utc_iso``); rows stamped by the
        digest are aware-UTC written naive, legacy rows may be naive local --
        same bounded display caveat as the analysis queue. Bookkeeping kinds
        (the per-row rejection coverage) are excluded: this is the log of emails
        SENT, one row per email."""
        rows = session.scalars(
            select(EmailLog)
            .where(EmailLog.kind.not_in(_BOOKKEEPING_EMAIL_KINDS))
            .order_by(EmailLog.sent_at.desc(), EmailLog.id.desc())
            .limit(limit))
        return {"emails": [{
            "id": m.id,
            "sent_at": _utc_iso(m.sent_at),
            "kind": m.kind,
            "subject": m.subject,
            "run_date": m.run_date.isoformat() if m.run_date is not None else None,
        } for m in rows]}

    return router


def _day_ts(d: date) -> datetime:
    """A DATE-only stamp anchored at end-of-day UTC -- the heartbeats'
    ``_eod_utc`` rule, restated over a non-optional date (that helper is
    ``date | None -> datetime | None``; every caller here holds a NOT NULL
    column, so the narrower signature keeps mypy honest without casts)."""
    return datetime.combine(d, time(23, 59), tzinfo=UTC)


def _aware(dt: datetime) -> datetime:
    """A stored datetime as aware-UTC for SORTING (DB datetimes come back naive;
    the writers stamp UTC -- the ``_utc_iso`` contract, needed here as a
    datetime, not a string, so the merge key can compare across sources)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

"""The deep-analysis surface: queue a request, list the queue, and the three
server-side byte proxies (analysis charts, the report PDF, a signal's chart).
Moved verbatim out of ``cockpit/api.py``; the resolver-security posture (only
DB-stored keys ever reach a resolver) is unchanged."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from swing_screener.cockpit.common import (
    _is_azure,
    _require_cockpit,
    _stored_error_detail,
    _utc_iso,
)
from swing_screener.db.models import Signal
from swing_screener.db.repo import (
    create_analysis_request,
    get_analysis_request,
    list_analysis_requests,
)
from swing_screener.storage.blob import resolve_chart_bytes, resolve_pdf_bytes


class AnalysisCreate(BaseModel):
    """POST /api/analysis body: one ticker, required, stripped + uppercased, and
    ASCII-only -- tickers are ASCII by construction, and a fullwidth look-alike
    (ＡＭＤ) would both miss the real symbol at fetch time and be stripped from the
    PDF filename downstream (see ``_pdf_filename``), so it is rejected at the front
    door. ``max_length`` mirrors ``AnalysisRequest.ticker``'s String(16) (the
    template rule); no float fields, so there is no ``allow_inf_nan`` to pin."""

    ticker: str = Field(max_length=16)

    @field_validator("ticker")
    @classmethod
    def _ticker_required_upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("ticker is required")
        if not v.isascii():
            raise ValueError("ticker must be ASCII")
        return v


def build_analysis_router(
    *,
    _session: Callable[[], Iterator[Session]],
    db_url: str,
) -> APIRouter:
    """The analysis endpoints, closed over the app's seams: the session dependency
    and the DB URL (``_worker_label`` derives who drains the queue from it)."""
    router = APIRouter()

    @router.post("/api/analysis", dependencies=[Depends(_require_cockpit)])
    def request_analysis(
        body: AnalysisCreate, session: Session = Depends(_session)
    ) -> dict[str, object]:
        """Queue an on-demand deep-analysis run for one ticker.

        Header-guarded (``_require_cockpit``); ``AnalysisCreate`` strips +
        uppercases (422 on empty/overlong). The SERVER stamps ``requested_at =
        datetime.now(UTC)`` -- the worker (``notify.ondemand``) claims and
        requeues by UTC comparison, so the client clock never ages a request.
        Queue-view reorder, disclosed: the retired Streamlit form stamped naive
        LOCAL time, and the list orders by the stored value, so old naive rows
        can sort out of true order against UTC stamps by up to the zone offset
        until they age out -- a one-time cosmetic reorder, not a processing
        change. The response echoes the aware stamp; the DB round-trips it
        tz-naive (UTC clock fields, see ``_stalled``)."""
        stamp = datetime.now(UTC)
        req = create_analysis_request(session, ticker=body.ticker, requested_at=stamp)
        return {"id": req.id, "ticker": req.ticker, "status": req.status,
                "requested_at": stamp.isoformat()}

    @router.get("/api/analysis")
    def analysis_list(
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """The deep-analysis queue, newest requested first, plus who drains it.

        ``stalled`` mirrors the worker's requeue window EXACTLY (running longer
        than ``_STALE_AFTER``): the next worker pass will requeue exactly those
        rows, so the UI can say 'stalled -- will retry' instead of spinning.
        ``worker`` derives from the DB URL (``_worker_label``): 'cloud (*/15min)'
        for Azure, else 'manual' -- the UI copy for manual says requests wait for
        ``python -m swing_screener.notify.ondemand``. Timestamps are served as
        unambiguous UTC ('+00:00'-suffixed, ``_utc_iso``): naive DB values are
        stamped UTC, because JS's ``Date()`` parses naive ISO as LOCAL and would
        skew every relative-time render by the zone offset. started_at and
        finished_at are worker-stamped UTC, so the stamp is unconditionally
        correct; legacy Streamlit ``requested_at`` rows were naive LOCAL and
        wear a bounded display offset until they age out (see the POST
        docstring). ``has_pdf``/``chart_count`` let the UI draw asset
        affordances without touching a resolver. Budget: one LIMITed SELECT,
        no resolver or network calls."""
        now = datetime.now(UTC)
        rows = list_analysis_requests(session, limit=limit)
        return {
            "requests": [{
                "id": r.id,
                "ticker": r.ticker,
                "status": r.status,
                "stalled": _stalled(r.status, r.started_at, now=now),
                "requested_at": _utc_iso(r.requested_at),
                "started_at": _utc_iso(r.started_at),
                "finished_at": _utc_iso(r.finished_at),
                "summary": r.summary,
                # whitelist, not sanitize: legacy pre-fix rows hold raw str(exc)
                "error": _stored_error_detail(r.error),
                "has_pdf": bool(r.pdf_blob_key),
                "chart_count": len(_chart_keys(r.chart_blob_keys)),
            } for r in rows],
            "worker": _worker_label(db_url),
        }

    @router.get("/api/analysis/{request_id}/chart/{index}")
    def analysis_chart(
        request_id: int, index: int, session: Session = Depends(_session)
    ) -> Response:
        """One of a request's chart PNGs, resolved SERVER-SIDE from the stored key.

        SECURITY: both path params are ints; the resolver receives ONLY the
        ``chart_blob_keys`` entry stored on the DB row -- no client-supplied key
        or path ever reaches a resolver (the arbitrary-read hole this closes).
        404 on an unknown id, an out-of-range index (negative included -- never
        end-relative), or an unresolvable key (aged-out blob / missing local
        file). Assets age out of the store, so a 404 here is a normal state."""
        req = get_analysis_request(session, request_id)
        if req is None:
            raise HTTPException(status_code=404, detail="unknown analysis request")
        keys = _chart_keys(req.chart_blob_keys)
        if not 0 <= index < len(keys):
            raise HTTPException(status_code=404, detail="no such chart")
        data = resolve_chart_bytes(keys[index])
        if data is None:
            raise HTTPException(status_code=404, detail="chart unavailable")
        return Response(content=data, media_type="image/png")

    @router.get("/api/analysis/{request_id}/pdf")
    def analysis_pdf(
        request_id: int, session: Session = Depends(_session)
    ) -> Response:
        """A request's report PDF, resolved SERVER-SIDE from the stored blob key.

        SECURITY: same posture as the chart proxy -- the resolver receives ONLY
        the row's ``pdf_blob_key``, never anything client-supplied. Served as an
        attachment named ``{ticker}_report.pdf`` (header-sanitized, see
        ``_pdf_filename``). 404 on an unknown id, a row with no PDF (queued /
        failed), or an unresolvable key -- aged-out assets are normal, not
        errors."""
        req = get_analysis_request(session, request_id)
        if req is None:
            raise HTTPException(status_code=404, detail="unknown analysis request")
        data = resolve_pdf_bytes(req.pdf_blob_key)
        if data is None:
            raise HTTPException(status_code=404, detail="pdf unavailable")
        disposition = f'attachment; filename="{_pdf_filename(req.ticker)}"'
        return Response(content=data, media_type="application/pdf",
                        headers={"Content-Disposition": disposition})

    @router.get("/api/signals/{signal_id}/chart")
    def signal_chart(
        signal_id: int, session: Session = Depends(_session)
    ) -> Response:
        """A signal's chart PNG, resolved SERVER-SIDE from ``Signal.chart_path``.

        SECURITY: the id is an int; the resolver receives ONLY the stored
        ``chart_path`` (blob key or local path) -- never a client value. 404 on
        an unknown id, a chartless signal (MOST signals -- the pipeline charts
        only surfaced picks, so 404 is the NORMAL case), or an unresolvable
        path."""
        sig = session.get(Signal, signal_id)
        if sig is None or sig.chart_path is None:
            raise HTTPException(status_code=404, detail="no chart for this signal")
        data = resolve_chart_bytes(sig.chart_path)
        if data is None:
            raise HTTPException(status_code=404, detail="chart unavailable")
        return Response(content=data, media_type="image/png")

    return router


# The on-demand worker's requeue window, RESTATED from ``notify.ondemand._STALE_AFTER``
# rather than imported: ondemand's module scope drags the pipeline + notify graph
# (pipeline.run, notify.analysis/pdf/transport) into the cockpit's import graph. A
# lockstep test (tests/cockpit/test_api.py) imports the real constant and pins equality.
_STALE_AFTER = timedelta(minutes=30)


def _stalled(status: str, started_at: datetime | None, *, now: datetime) -> bool:
    """True when a 'running' row has exceeded the worker's requeue window: the next
    worker pass will flip exactly these rows back to 'queued'
    (``repo.requeue_stale_running`` at ``now - _STALE_AFTER``), so the UI says
    'stalled -- will retry' instead of spinning forever. Stored datetimes come back
    NAIVE (sqlite/mssql DATETIME drop tzinfo); the worker stamps UTC, so naive is
    read as UTC."""
    if status != "running" or started_at is None:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return (now - started_at) > _STALE_AFTER


def _worker_label(db_url: str) -> str:
    """Who drains the queue, derived from the DB URL (the ``_is_azure`` predicate --
    URL-shaped, never connectivity-shaped): the Azure DB is drained by the cloud
    job every 15 minutes; a local DB has NO scheduled drain, so requests wait for a
    manual ``python -m swing_screener.notify.ondemand`` run (the UI copy says so)."""
    return "cloud (*/15min)" if _is_azure(db_url) else "manual"


def _chart_keys(raw: str) -> list[str]:
    """``AnalysisRequest.chart_blob_keys`` is COMMA-JOINED (String(2048)): split,
    strip, drop empties -- a trailing comma or blank segment is not a chart."""
    return [k.strip() for k in raw.split(",") if k.strip()]


def _pdf_filename(ticker: str) -> str:
    """``{ticker}_report.pdf`` with the ticker reduced to header-safe characters:
    the value is DB-sourced (model-validated on the way in today, but legacy rows
    predate the model) and a quote or CR/LF inside Content-Disposition corrupts the
    header. ASCII alphanumerics plus ``._-`` survive -- ``isalnum`` alone is
    Unicode-aware, so a fullwidth ticker (ＡＭＤ) would sail through the allowlist
    and then blow up starlette's latin-1 header encoding as an unhandled 500,
    inside this sanitizer's own threat model. An emptied ticker reads 'analysis'."""
    safe = "".join(c for c in ticker if c.isascii() and (c.isalnum() or c in "._-"))
    return f"{safe or 'analysis'}_report.pdf"

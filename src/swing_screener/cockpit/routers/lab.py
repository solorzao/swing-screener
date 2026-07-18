"""TICKER LAB cockpit router: the on-demand per-ticker technical study.

Two surfaces. ``GET /api/lab/bars`` is a read-only on-demand fetch (the gex
build posture: nothing touches the network until the user asks; a dead upstream
is a 503 wearing the exception CLASS only) that returns the deterministic study
for one (ticker, timeframe): HA + real candles, EMA 9/21/50/200, MACD(12,26,9),
volume, swing-pivot S/R and Fibonacci levels -- all plain nullable numbers
(facts, never Stats). It persists nothing, so it bumps no nonce and needs no
watermark.

``POST /api/lab/analysis`` queues an Opus deep-analysis note over the full
four-timeframe study and drains it IMMEDIATELY on an in-process daemon thread --
the lab is interactive research, not a scheduled digest, so it must work on a
local box with no cloud worker. The thread lazy-imports the analyst stack
(anthropic + notify) so the cockpit's module import graph stays light, stores
only exception CLASS names, and bumps the action nonce after each durable write
so every window wakes.
"""

import logging
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from swing_screener.cockpit.common import (
    ActionNonce,
    _require_cockpit,
    _stored_error_detail,
    _utc_iso,
)
from swing_screener.cockpit.routers.analysis import AnalysisCreate
from swing_screener.db.repo import (
    complete_lab_analysis,
    create_lab_analysis,
    fail_lab_analysis,
    get_lab_analysis,
    list_lab_analyses,
)
from swing_screener.lab import LAB_TIMEFRAMES, build_lab_payload, fetch_lab_frame
from swing_screener.lab.report import lab_facts_text
from swing_screener.settings import load_settings

log = logging.getLogger(__name__)

# The routine upstream failure classes on the on-demand bar fetch (the gex
# posture, restated: yfinance transport errors are OSError subclasses; the lab
# fetcher raises RuntimeError on empty/exhausted retries). Anything else is a
# genuine bug and still 500s; SQLAlchemyError rides the app-level handler.
_UPSTREAM_ERRORS = (OSError, RuntimeError)

# The in-process drain has NO requeue pass (unlike notify.ondemand): if the
# cockpit dies mid-run the row stays 'running' forever. After this window the
# UI stops saying 'analyzing' and says re-request instead.
_LAB_STALE_AFTER = timedelta(minutes=10)

# (ticker, timeframe) -> OHLCV frame; raises RuntimeError/OSError on upstream
# failure. The create_app test seam replaces it so the suite never hits yfinance.
LabBars = Callable[[str, str], pd.DataFrame]


def _upstream_503(exc: Exception) -> HTTPException:
    """The upstream-failure wire shape: 503, class name only, never the message."""
    return HTTPException(
        status_code=503, detail=f"upstream error ({type(exc).__name__})")


def _clean_ticker(raw: str) -> str:
    """Query-param twin of ``AnalysisCreate``'s validator: strip/upper/ASCII/<=16,
    422 on violation (a malformed query value is a client error, never a 500)."""
    v = raw.strip().upper()
    if not v:
        raise HTTPException(status_code=422, detail="ticker is required")
    if not v.isascii():
        raise HTTPException(status_code=422, detail="ticker must be ASCII")
    if len(v) > 16:
        raise HTTPException(status_code=422, detail="ticker must be <= 16 characters")
    return v


def _default_bars() -> LabBars:
    """Bind the real per-timeframe fetch over the settings bar-cache dir (read
    once, at router-build time -- the ``_default_quote_fetch`` pattern)."""
    cache_dir = load_settings().cache_dir

    def bars(ticker: str, timeframe: str) -> pd.DataFrame:
        return fetch_lab_frame(ticker, timeframe, cache_dir=cache_dir)

    return bars


def _build_default_worker(
    _engine: Callable[[], Engine], bars: LabBars, action_nonce: ActionNonce,
) -> Callable[[int], None]:
    """The real in-process drain for one queued lab analysis. NEVER raises: any
    failure flips the row to 'failed' storing the exception CLASS name only.
    The analyst stack (anthropic + notify) is imported lazily inside the run so
    the cockpit module graph stays light and a broken optional dep surfaces as a
    failed row, not a dead app."""

    def run(analysis_id: int) -> None:
        with Session(_engine()) as session:
            row = get_lab_analysis(session, analysis_id)
            if row is None or row.status != "queued":
                return
            ticker = row.ticker
            model, reasoning = row.model, row.reasoning
            row.status = "running"
            row.started_at = datetime.now(UTC)
            session.commit()
        action_nonce.bump()  # the started_at write is durable; wake other windows
        try:
            payloads = {}
            for tf in LAB_TIMEFRAMES:
                try:
                    payloads[tf] = build_lab_payload(ticker, tf, bars(ticker, tf))
                except Exception:
                    # Per-timeframe isolation: the facts block names the miss.
                    log.warning("lab frame %s %s failed", ticker, tf, exc_info=True)
            if not payloads:
                raise RuntimeError(f"no timeframes fetched for {ticker}")
            facts = lab_facts_text(payloads)

            from swing_screener.notify.market_context import (
                context_block,
                get_fundamentals,
                get_recent_news,
            )

            context = context_block(get_fundamentals(ticker), get_recent_news(ticker))

            from swing_screener.notify.analysis import analyze_lab_deep

            settings = load_settings()
            result = analyze_lab_deep(
                ticker, facts, context_text=context, model=model,
                reasoning=reasoning,
                max_searches=settings.analysis_max_searches,
            )
            with Session(_engine()) as session:
                complete_lab_analysis(
                    session, analysis_id, report=result.report,
                    is_deep=result.is_deep, finished_at=datetime.now(UTC),
                    est_cost_usd=(
                        result.usage.est_cost_usd if result.usage is not None else None
                    ),
                )
        except Exception as exc:
            log.warning("lab analysis %s failed", analysis_id, exc_info=True)
            try:
                with Session(_engine()) as session:
                    # Class name only -- fetcher/SDK messages can embed hosts/keys.
                    fail_lab_analysis(
                        session, analysis_id, error=f"error ({type(exc).__name__})",
                        finished_at=datetime.now(UTC),
                    )
            except Exception:
                log.warning("lab analysis %s failure not recorded", analysis_id,
                            exc_info=True)
        action_nonce.bump()  # terminal write (done/failed) is durable

    return run


def build_lab_router(
    *,
    _engine: Callable[[], Engine],
    _session: Callable[[], Iterator[Session]],
    action_nonce: ActionNonce,
    bars: LabBars | None = None,
    worker: Callable[[int], None] | None = None,
) -> APIRouter:
    """The lab endpoints, closed over the app's seams. ``bars`` and ``worker``
    are the test seams: ``None`` binds the real yfinance fetch and the real
    in-process Opus drain."""
    router = APIRouter()
    resolved_bars = bars if bars is not None else _default_bars()
    resolved_worker = (
        worker if worker is not None
        else _build_default_worker(_engine, resolved_bars, action_nonce)
    )

    @router.get("/api/lab/bars")
    def lab_bars(
        ticker: str = Query(max_length=32),
        timeframe: str = Query(default="1d", max_length=8),
    ) -> dict[str, object]:
        """The deterministic study for one (ticker, timeframe): candles (real +
        HA), EMA 9/21/50/200, MACD, volume, S/R and Fib levels. On-demand fetch
        (a cold ticker blocks a threadpool worker for a few seconds -- the
        accepted gex/livedata posture); repeat calls ride the per-day parquet
        cache. Read-only: no DB, no nonce, no watermark. 4h serves at most ~60
        days (the yfinance 1h window the 4h resample rides on)."""
        symbol = _clean_ticker(ticker)
        if timeframe not in LAB_TIMEFRAMES:
            raise HTTPException(
                status_code=422,
                detail=f"timeframe must be one of {', '.join(LAB_TIMEFRAMES)}")
        try:
            frame = resolved_bars(symbol, timeframe)
            return build_lab_payload(symbol, timeframe, frame)
        except _UPSTREAM_ERRORS as exc:
            raise _upstream_503(exc) from exc

    @router.post("/api/lab/analysis", dependencies=[Depends(_require_cockpit)])
    def request_lab_analysis(
        body: AnalysisCreate, session: Session = Depends(_session)
    ) -> dict[str, object]:
        """Queue the Opus deep analysis over the full four-timeframe study and
        start the in-process drain immediately (daemon thread -- the POST stays
        instant; the Anthropic key stays server-side). Header-guarded; the
        SERVER stamps ``requested_at``; model + effort are frozen onto the row
        at queue time so the report is attributable to what actually ran."""
        settings = load_settings()
        stamp = datetime.now(UTC)
        row = create_lab_analysis(
            session, ticker=body.ticker, requested_at=stamp,
            model=settings.analysis_model, reasoning=settings.lab_reasoning,
        )
        action_nonce.bump()  # the queued row is durable
        threading.Thread(
            target=resolved_worker, args=(row.id,), daemon=True,
            name=f"lab-analysis-{row.id}",
        ).start()
        return {"id": row.id, "ticker": row.ticker, "status": row.status,
                "requested_at": stamp.isoformat(), "model": row.model,
                "reasoning": row.reasoning}

    @router.get("/api/lab/analysis")
    def lab_analysis_list(
        ticker: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """Lab deep-analysis notes, newest first, optionally one ticker's.

        The FULL markdown report rides the row (the lab renders it inline --
        no blob, no PDF). ``stalled`` means the in-process drain ran past
        ``_LAB_STALE_AFTER`` -- with no requeue pass, that almost always means
        the cockpit restarted mid-run and the honest copy is 'request again',
        never a spinner. ``est_cost_usd`` is the uncapped path's cost
        visibility: null when no billed call was captured, never a fake $0.
        Timestamps serve as '+00:00' UTC (``_utc_iso``)."""
        symbol = _clean_ticker(ticker) if ticker is not None else None
        now = datetime.now(UTC)
        rows = list_lab_analyses(session, ticker=symbol, limit=limit)
        return {
            "analyses": [{
                "id": r.id,
                "ticker": r.ticker,
                "status": r.status,
                "stalled": _stalled(r.status, r.started_at, now=now),
                "requested_at": _utc_iso(r.requested_at),
                "started_at": _utc_iso(r.started_at),
                "finished_at": _utc_iso(r.finished_at),
                "model": r.model,
                "reasoning": r.reasoning,
                "is_deep": r.is_deep,
                "report": r.report,
                # whitelist, not sanitize (the stored-error leak posture)
                "error": _stored_error_detail(r.error),
                "est_cost_usd": r.est_cost_usd,
            } for r in rows],
        }

    return router


def _stalled(status: str, started_at: datetime | None, *, now: datetime) -> bool:
    """True when a 'running' row outlived the in-process drain window (see
    ``_LAB_STALE_AFTER``). Stored datetimes round-trip NAIVE; the drain stamps
    UTC, so naive reads as UTC (the analysis-router convention)."""
    if status != "running" or started_at is None:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return (now - started_at) > _LAB_STALE_AFTER

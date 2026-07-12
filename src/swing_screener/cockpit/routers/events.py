"""The SSE wake channel (``/api/events``) and its change token. Moved verbatim out
of ``cockpit/api.py``.

NOTHING ELSE LIVES HERE, deliberately: Task 12 (Phase 3 plan) grows the SSE
endpoint and the token's watermark set together, so this module is their one home
-- keep new watermarks and stream behavior in this file, not in api.py."""

import json
from collections.abc import AsyncIterator, Callable
from datetime import date
from pathlib import Path

import anyio.to_thread
from fastapi import APIRouter
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from swing_screener.cockpit.heartbeats import newest_verdicts_mtime
from swing_screener.db.models import (
    EmailLog,
    ExitEvent,
    MarketReport,
    PaperTrade,
    Signal,
)
from swing_screener.db.repo import latest_reversal_funnel
from swing_screener.settings import resolve_edge_dir


def build_events_router(
    *,
    _engine: Callable[[], Engine],
    edge_dir: Path | None,
) -> APIRouter:
    """The wake-channel endpoint, closed over the app's seams: the cached engine
    accessor (re-probed per tick via ``_safe_change_token``) and the raw edge dir
    (resolved per token read, same seam as the heartbeats)."""
    router = APIRouter()

    @router.get("/api/events")
    async def events() -> EventSourceResponse:
        """The SSE wake channel: a ``change`` event whenever the change token moves
        -- plus ALWAYS one on (re)connect, since ``last`` starts None. Consumers
        (Task 9's useEventWake) must treat an event as a refetch trigger, never as
        evidence something changed.

        Data changes come from EXTERNAL processes (scheduled jobs, git pulls), so
        server-side polling is the only correct driver -- there is no in-process
        write to hook. The endpoint is async so the infinite generator never pins a
        threadpool worker; each token read hops through ``anyio.to_thread`` (the DB
        probe is sync) and no Session survives across the sleeps. The frontend's 60s
        poll stays the floor -- this channel only wakes it early: the poll is the
        defense against dropped SSE connections and change classes the token doesn't
        watch. GET under /api (covered by the dev vite proxy); no X-Cockpit header:
        it mutates nothing.
        """

        async def stream() -> AsyncIterator[dict[str, str]]:
            last: dict[str, str] | None = None
            while True:
                token = await anyio.to_thread.run_sync(
                    _safe_change_token, _engine, edge_dir
                )
                if token != last:
                    last = token
                    yield {"event": "change", "data": json.dumps(token)}
                await anyio.sleep(_WAKE_POLL_S)

        return EventSourceResponse(stream(), ping=_WAKE_POLL_S)

    return router


# One cadence for the wake channel: the token poll AND sse-starlette's keepalive
# ping tick together, deliberately -- a ping without a fresh token read (or vice
# versa) buys nothing, so the two must not drift apart.
_WAKE_POLL_S = 15


def _watermark(value: object | None) -> str:
    """One watermark's wire form: ISO for dates/datetimes, ``str()`` for ids, the
    literal ``"none"`` for an empty table -- strings only, so token equality is a
    plain dict compare and ``json.dumps`` never meets a date object."""
    if value is None:
        return "none"
    if isinstance(value, date):  # datetime is a date subclass; isoformat covers both
        return value.isoformat()
    return str(value)


def _change_token(engine: Engine, edge_dir: Path) -> dict[str, str]:
    """The wake channel's change token: cheap max-watermarks over every store the
    cockpit renders -- screen run, paper-trade write, trade close, sent email,
    market report, funnel snapshot, newest reflection verdicts file. Equality means
    "nothing worth refetching"; the values are opaque to the frontend. The ``exit``
    watermark exists because a close is an UPDATE on paper_trades (no new id, no
    updated_at column) -- invisible to ``max(PaperTrade.id)`` -- but every close
    path (shadow.py, reconcile.py, exitcheck.py) INSERTS an ExitEvent. One
    short-lived Session per call, never held across the stream loop's sleeps.
    ``edge_dir`` is the RESOLVED edge directory (the caller threads
    ``resolve_edge_dir`` -- same seam as the heartbeats)."""
    with Session(engine) as session:
        funnel = latest_reversal_funnel(session)
        token = {
            "signal": _watermark(session.scalar(select(func.max(Signal.run_date)))),
            "trade": _watermark(session.scalar(select(func.max(PaperTrade.id)))),
            "exit": _watermark(session.scalar(select(func.max(ExitEvent.id)))),
            "email": _watermark(session.scalar(select(func.max(EmailLog.sent_at)))),
            "weather": _watermark(
                session.scalar(select(func.max(MarketReport.run_date)))
            ),
            "funnel": _watermark(funnel.run_date if funnel is not None else None),
        }
    token["verdicts"] = _watermark(newest_verdicts_mtime(edge_dir))
    return token


def _safe_change_token(
    engine_factory: Callable[[], Engine], edge_dir: Path | None
) -> dict[str, str]:
    """``_change_token`` with the down-DB posture: ANY failure (engine creation,
    query, filesystem) collapses to the sentinel ``{"db": "down"}`` -- the stream
    loop must never die, and recovery reads as a change (the sentinel can never
    equal a real token). The factory is the app's cached ``_engine`` accessor, so
    the down case is re-probed per tick, never latched."""
    try:
        return _change_token(engine_factory(), resolve_edge_dir(edge_dir))
    except Exception:
        return {"db": "down"}

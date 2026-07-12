"""The SSE wake channel (``/api/events``) and its change token. Moved verbatim out
of ``cockpit/api.py``.

NOTHING ELSE LIVES HERE, deliberately: Task 12 (Phase 3 plan) grew the SSE
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

from swing_screener.cockpit.common import ActionNonce
from swing_screener.cockpit.heartbeats import newest_verdicts_mtime
from swing_screener.db.models import (
    AnalysisRequest,
    AnalystCall,
    EmailLog,
    ExecutionLog,
    ExitEvent,
    MarketReport,
    PaperTrade,
    Signal,
    Trade,
)
from swing_screener.db.repo import latest_reversal_funnel
from swing_screener.pipeline.proposed import store_filename
from swing_screener.settings import resolve_edge_dir


def build_events_router(
    *,
    _engine: Callable[[], Engine],
    edge_dir: Path | None,
    action_nonce: ActionNonce,
) -> APIRouter:
    """The wake-channel endpoint, closed over the app's seams: the cached engine
    accessor (re-probed per tick via ``_safe_change_token``), the raw edge dir
    (resolved per token read, same seam as the heartbeats), and the app's
    post-action nonce (the same instance every action POST bumps)."""
    router = APIRouter()

    @router.get("/api/events")
    async def events() -> EventSourceResponse:
        """The SSE wake channel: a ``change`` event whenever the change token moves
        -- plus ALWAYS one on (re)connect, since ``last`` starts None. Consumers
        (Task 9's useEventWake) must treat an event as a refetch trigger, never as
        evidence something changed.

        Data changes come from EXTERNAL processes (scheduled jobs, git pulls), so
        server-side polling is the only correct driver -- there is no in-process
        write to hook. The one IN-process write class -- the cockpit's own action
        POSTs -- rides the same poll via the action nonce: a successful action
        bumps it, the token moves on the next tick, and every other window wakes
        within ``_WAKE_POLL_S`` instead of its 60s poll floor (the acting window
        refetches locally and never waits on this channel). The endpoint is async
        so the infinite generator never pins a threadpool worker; each token read
        hops through ``anyio.to_thread`` (the DB probe is sync) and no Session
        survives across the sleeps. The frontend's 60s poll stays the floor --
        this channel only wakes it early: the poll is the defense against dropped
        SSE connections and change classes the token doesn't watch. GET under
        /api (covered by the dev vite proxy); no X-Cockpit header: it mutates
        nothing.
        """

        async def stream() -> AsyncIterator[dict[str, str]]:
            last: dict[str, str] | None = None
            while True:
                token = await anyio.to_thread.run_sync(
                    _safe_change_token, _engine, edge_dir, action_nonce
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


def _file_watermark(path: Path) -> str:
    """One file's change watermark: ``st_mtime_ns`` (integer nanoseconds -- an
    in-place atomic rewrite within the same clock second must still move it,
    which the float ``st_mtime`` can't promise), or ``"none"`` for a missing
    file. Any stat failure reads as missing: a transiently unreadable file must
    degrade to a watermark value, never kill the stream loop's token read."""
    try:
        return str(path.stat().st_mtime_ns)
    except OSError:
        return "none"


def _change_token(engine: Engine, edge_dir: Path) -> dict[str, str]:
    """The wake channel's change token: cheap max-watermarks over every store the
    cockpit renders -- screen run, paper-trade write, trade close, sent email,
    market report, funnel snapshot, plus (Task 12) everything the six actions
    touch: the real book, the analysis queue's whole lifecycle, the execution
    log, analyst calls and their scoring, and the edge-dir decision files.
    Equality means "nothing worth refetching"; the values are opaque to the
    frontend. Scalar max/count selects only -- nothing here drags a table.

    UPDATE-shaped changes need non-id watermarks, each spelled out:

    * ``exit``: a close is an UPDATE on paper_trades AND on trades (no new id,
      no updated_at column) -- invisible to ``max(id)`` -- but every close path
      (shadow.py, reconcile.py, exitcheck.py, and the cockpit's manual close)
      INSERTS an ExitEvent, so ``max(ExitEvent.id)`` is the close clock for
      BOTH books.
    * ``analysis`` pipes four components: ``max(id)`` (a new request),
      ``max(finished_at)`` (a completion), ``max(started_at)`` (a claim stamps
      now -- always the newest stamp, so every queued->running flip moves it),
      and ``count(started_at not null)`` -- the requeue clock. A requeue
      (running->queued) NULLS started_at, which ``max(started_at)`` alone can
      miss: when another row holds a later stamp the max simply doesn't move.
      The count always drops by exactly the requeued rows. (A same-tick claim
      + requeue offsets the count, but the claim's fresh stamp moves the max
      -- the pair covers every transition.)
    * ``analyst``: scoring is an UPDATE (scored_at set on an existing row), so
      ``count(scored_at not null)`` rides beside ``max(id)``.

    One short-lived Session per call, never held across the stream loop's
    sleeps. ``edge_dir`` is the RESOLVED edge directory (the caller threads
    ``resolve_edge_dir`` -- same seam as the heartbeats); the proposal stores
    and the experiment registry are file mtimes there, like the verdicts."""
    with Session(engine) as session:
        funnel = latest_reversal_funnel(session)
        token = {
            "signal": _watermark(session.scalar(select(func.max(Signal.run_date)))),
            "trade": _watermark(session.scalar(select(func.max(PaperTrade.id)))),
            "trade_real": _watermark(session.scalar(select(func.max(Trade.id)))),
            "exit": _watermark(session.scalar(select(func.max(ExitEvent.id)))),
            "email": _watermark(session.scalar(select(func.max(EmailLog.sent_at)))),
            "weather": _watermark(
                session.scalar(select(func.max(MarketReport.run_date)))
            ),
            "funnel": _watermark(funnel.run_date if funnel is not None else None),
            "analysis": "|".join((
                _watermark(session.scalar(select(func.max(AnalysisRequest.id)))),
                _watermark(
                    session.scalar(select(func.max(AnalysisRequest.finished_at)))
                ),
                _watermark(
                    session.scalar(select(func.max(AnalysisRequest.started_at)))
                ),
                _watermark(session.scalar(
                    select(func.count(AnalysisRequest.id))
                    .where(AnalysisRequest.started_at.is_not(None))
                )),
            )),
            "execution": _watermark(
                session.scalar(select(func.max(ExecutionLog.id)))
            ),
            "analyst": "|".join((
                _watermark(session.scalar(select(func.max(AnalystCall.id)))),
                _watermark(session.scalar(
                    select(func.count(AnalystCall.id))
                    .where(AnalystCall.scored_at.is_not(None))
                )),
            )),
        }
    token["verdicts"] = _watermark(newest_verdicts_mtime(edge_dir))
    token["proposals"] = "|".join(
        _file_watermark(edge_dir / store_filename(pt))
        for pt in ("continuation", "reversal")
    )
    token["registry"] = _file_watermark(edge_dir / "experiments.json")
    return token


def _safe_change_token(
    engine_factory: Callable[[], Engine],
    edge_dir: Path | None,
    action_nonce: ActionNonce,
) -> dict[str, str]:
    """``_change_token`` with the down-DB posture: ANY failure (engine creation,
    query, filesystem) collapses to the sentinel ``{"db": "down"}`` -- the stream
    loop must never die, and recovery reads as a change (the sentinel can never
    equal a real token). The factory is the app's cached ``_engine`` accessor, so
    the down case is re-probed per tick, never latched.

    The action nonce rides OUTSIDE the try, on both shapes: it is pure
    in-process state (a lock and an int -- it cannot raise), and it must keep
    waking pollers even while the DB is down -- proposal decisions are
    file-only writes that still succeed then, and their wake must not be
    swallowed by the sentinel."""
    try:
        token = _change_token(engine_factory(), resolve_edge_dir(edge_dir))
    except Exception:
        token = {"db": "down"}
    token["action"] = _watermark(action_nonce.value)
    return token

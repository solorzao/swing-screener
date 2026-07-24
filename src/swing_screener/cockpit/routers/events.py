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
from swing_screener.db.models import (
    AnalysisRequest,
    AnalystCall,
    EmailLog,
    ExecutionLog,
    ExitEvent,
    GexSnapshot,
    JournalNote,
    JournalReview,
    JournalThesis,
    JournalTradeTag,
    LabAnalysis,
    MarketReport,
    OptionPaperTrade,
    OptionSetup,
    PaperTrade,
    Signal,
    SystemAudit,
    Trade,
)
from swing_screener.db.repo import latest_reversal_funnel
from swing_screener.pipeline.proposed import PLAY_TYPES, store_filename
from swing_screener.pipeline.reflect import verdicts_filename
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
    ``resolve_edge_dir`` -- same seam as the heartbeats); the verdicts
    sidecars, proposal stores, and experiment registry are ``_file_watermark``
    ns-mtimes there. The verdicts key deliberately does NOT reuse the
    heartbeat's ``newest_verdicts_mtime`` (float mtime, and the heartbeat
    genuinely wants a datetime): reflect's ``--verdicts-only`` mode REWRITES
    ``edge/<pt>.verdicts.json`` in place, and only the integer-ns clock
    promises same-second rewrite detection."""
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
            # TICKER LAB deep analyses: the in-process drain claims (started_at
            # UPDATE) and completes (finished_at UPDATE) without new ids, so both
            # ride beside max(id) -- the analysis-queue idiom. The acting window
            # also gets the action-nonce bump; this covers every OTHER window and
            # a second cockpit process on the same DB.
            "lab": "|".join((
                _watermark(session.scalar(select(func.max(LabAnalysis.id)))),
                _watermark(
                    session.scalar(select(func.max(LabAnalysis.finished_at)))
                ),
                _watermark(session.scalar(
                    select(func.count(LabAnalysis.id))
                    .where(LabAnalysis.started_at.is_not(None))
                )),
            )),
            "analyst": "|".join((
                _watermark(session.scalar(select(func.max(AnalystCall.id)))),
                _watermark(session.scalar(
                    select(func.count(AnalystCall.id))
                    .where(AnalystCall.scored_at.is_not(None))
                )),
            )),
            # Journal annotations are append-only (tags via get-or-create insert,
            # notes/theses accrued), so max(id) is a sufficient wake clock for each --
            # a new note/tag/thesis from any process moves the token. The cockpit's own
            # note/tag POSTs ALSO ride the action nonce; these cover external writers.
            "journal_notes": _watermark(
                session.scalar(select(func.max(JournalNote.id)))
            ),
            "journal_tags": _watermark(
                session.scalar(select(func.max(JournalTradeTag.id)))
            ),
            "journal_theses": _watermark(
                session.scalar(select(func.max(JournalThesis.id)))
            ),
            # GEX options lab (module 2). max(id) catches new snapshots, setups, and
            # opened trades; max(closed_at) is the settle clock -- a nightly settle
            # CLOSES a lab trade in place (UPDATE, no new id), like the exit clock.
            "gex": "|".join((
                _watermark(session.scalar(select(func.max(GexSnapshot.id)))),
                _watermark(session.scalar(select(func.max(OptionSetup.id)))),
                _watermark(session.scalar(select(func.max(OptionPaperTrade.id)))),
                _watermark(session.scalar(select(func.max(OptionPaperTrade.closed_at)))),
            )),
            # Journal v2. A coach review's narrative is BACKFILLED async (UPDATE, no
            # new id) and its human_edit is an UPDATE too, so max(id) alone is blind --
            # pipe counts of the non-null columns beside it (the analyst-scoring idiom).
            "coach_reviews": "|".join((
                _watermark(session.scalar(select(func.max(JournalReview.id)))),
                _watermark(session.scalar(select(func.count(JournalReview.id))
                                          .where(JournalReview.narrative.is_not(None)))),
                _watermark(session.scalar(select(func.count(JournalReview.id))
                                          .where(JournalReview.human_edit.is_not(None)))),
            )),
            # A system audit's acknowledged_by_human flag is an UPDATE -> pipe its count.
            # `== True` renders `= 1`; `.is_(True)` renders `IS 1`, which SQL Server
            # rejects -- and this token is computed by the desktop app against Azure SQL.
            "system_audits": "|".join((
                _watermark(session.scalar(select(func.max(SystemAudit.id)))),
                _watermark(session.scalar(select(func.count(SystemAudit.id))
                                          .where(SystemAudit.acknowledged_by_human == True))),
            )),
        }
    token["verdicts"] = "|".join(
        _file_watermark(edge_dir / verdicts_filename(pt)) for pt in PLAY_TYPES
    )
    token["proposals"] = "|".join(
        _file_watermark(edge_dir / store_filename(pt)) for pt in PLAY_TYPES
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
    except Exception:  # noqa: BLE001 -- best-effort DB probe; sentinel on any failure
        token = {"db": "down"}
    token["action"] = _watermark(action_nonce.value)
    return token

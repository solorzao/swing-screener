"""The execution-safety surface: the advisory gate, the DISARM runbook action, the
Execution Safety report, and (Task 15) the guardrails brake's own read/write
endpoints. Moved verbatim out of ``cockpit/api.py``; lock semantics (single-flight,
non-blocking acquire, release in the outer ``finally``) and every status code are
unchanged."""

import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from swing_screener.cockpit.common import (
    ActionNonce,
    _armed_symbols,
    _bracket,
    _require_cockpit,
    _utc_iso,
)
from swing_screener.cockpit.livedata import BrokerSnapshot, Snapshot
from swing_screener.cockpit.spend import spend_rows_since
from swing_screener.db import guardrails_repo
from swing_screener.db.guardrails_repo import GuardrailsState
from swing_screener.db.models import AgentGuardrailEvent, DisarmEvent
from swing_screener.db.repo import latest_recorded_stop, latest_run_date
from swing_screener.pipeline import guardrails as gpipe
from swing_screener.pipeline.autonomy import autonomy_gate, gate_countdown
from swing_screener.pipeline.broker import BrokerClient, BrokerOrder, broker_error_detail
from swing_screener.pipeline.disarm import ensure_stop_protection, pull_entry_orders
from swing_screener.pipeline.preflight import PreflightReport, preflight
from swing_screener.config import StrategyConfig
from swing_screener.options.config import GexConfig
from swing_screener.settings import (
    load_settings,
    real_money_limits_ok,
    resolve_edge_dir,
    resolve_execution,
    resolve_risk_unit,
)


log = logging.getLogger(__name__)

#: The POST body keys that are NOT breaker limits. Everything else the body carried
#: is forwarded to ``guardrails_repo.edit_limits``, whose whitelist is the ONE gate
#: (a restated copy here could drift from what actually protects the state columns) --
#: which is also why an unknown key reaches the operator wearing the repo's own
#: ValueError text rather than a re-worded cockpit message.
_NON_LIMIT_KEYS = frozenset({"action", "ack_trip_id"})

#: How much guardrail history the panel gets in one read. The table is append-only and
#: low-volume (one row per operator action / trip / sweep), so 25 covers "what just
#: happened" without paging machinery the brake does not need.
_EVENT_HISTORY = 25

#: The no-broker wording, shared with /api/disarm's 409 so the two surfaces can never
#: describe the same configuration state in two different ways.
_NO_BROKER = "no broker configured"


def _halt_key() -> str:
    """The HALT sweep's ``key_suffix``: ``halt-cockpit-<UTC to the second>``.

    Mirrors the digest's manual-HALT sweep (``halt-{run_date}``) with the cockpit's
    per-second stamp, so re-submits inside the same second collapse at the venue.
    Cross-process collapse is NOT the goal here -- that is what the trip's
    ``guardrail-{trip_id}`` key is for, and why /api/disarm routes a tripped book
    through the resume instead of a fresh suffix."""
    return f"halt-cockpit-{datetime.now(UTC):%Y%m%d%H%M%S}"


def _sweep_detail(session: Session) -> str:
    """The newest recorded ``sweep`` event's detail, VERBATIM.

    The resume path cannot itemize what it moved (``resume_incomplete_sweep``
    returns a bool), but the sweep it just ran recorded its own summary -- "swept: N
    entry order(s) cancelled, M protective stop(s) restored", or a class-name-only
    error on a partial -- as the event's reason. Echoing that string is strictly
    better than restating it: it is the SAME text the Auditor and the event history
    show, so the three can never disagree. Newest-row rather than a trip_id filter
    because the sweep was written moments ago in THIS request; the only way another
    row interleaves is a concurrent process sweeping the same in-force trip, whose
    summary describes the same work."""
    reason = session.scalar(
        select(AgentGuardrailEvent.reason)
        .where(AgentGuardrailEvent.kind == "sweep")
        .order_by(AgentGuardrailEvent.id.desc())
        .limit(1))
    return reason or "the sweep outcome was not recorded -- see the guardrail events"


class GuardrailAction(BaseModel):
    """POST /api/guardrails body: one ``action`` plus whatever that action needs.

    ``extra='allow'`` is DELIBERATE. The six limit columns are declared (so the wire
    gets real coercion -- a date string becomes a date, a non-finite float is a 422)
    but anything else the client sent still rides through to ``edit_limits``, whose
    whitelist refuses it by name. That keeps ONE definition of "editable" in the repo
    and makes ``{"action": "edit", "state": "ok"}`` a 422 that SAYS ``state`` is not
    an editable limit column -- rather than a silently-ignored field.

    Presence, not value, decides what an edit touches: the handler reads
    ``model_fields_set``, so an omitted key is left alone and an explicit ``null``
    UNSETS the breaker (the legal way to disarm one). ``hwm_baseline_usd`` is
    therefore typed NON-optional: the column is NOT NULL, so a null there is a client
    error (422) and never an IntegrityError wearing a 503.
    """

    model_config = ConfigDict(extra="allow")

    action: Literal["edit", "halt", "clear_halt", "clear_trip"]
    #: clear_trip only: the trip id the operator actually acknowledged.
    ack_trip_id: int | None = None
    max_daily_loss_usd: float | None = Field(default=None, allow_inf_nan=False)
    max_trades_per_day: int | None = None
    max_drawdown_usd: float | None = Field(default=None, allow_inf_nan=False)
    loss_streak_halt: int | None = None
    hwm_anchor_date: date | None = None
    hwm_baseline_usd: float = Field(default=0.0, allow_inf_nan=False)


def _state_body(g: GuardrailsState) -> dict[str, object]:
    """The brake row on the wire: every ``GuardrailsState`` field, FLAT and under its
    OWN column name -- the same keys the POST edit body accepts, so the panel's form
    round-trips without a translation layer (and a new column can never be silently
    dropped by a hand-maintained mapping)."""
    return {
        "state": g.state,
        "max_daily_loss_usd": g.max_daily_loss_usd,
        "max_trades_per_day": g.max_trades_per_day,
        "max_drawdown_usd": g.max_drawdown_usd,
        "loss_streak_halt": g.loss_streak_halt,
        "hwm_anchor_date": g.hwm_anchor_date.isoformat() if g.hwm_anchor_date else None,
        "hwm_baseline_usd": g.hwm_baseline_usd,
        "trip_id": g.trip_id,
        "trip_reason": g.trip_reason,
        "sweep_state": g.sweep_state,
    }


def _current_breach(session: Session, g: GuardrailsState) -> dict[str, str] | None:
    """``{breaker, reason}`` for the first breaker breached RIGHT NOW, else None.

    ``guardrails_repo.breached_breaker`` over a snapshot the caller already holds --
    the PURE half of ``pipeline.guardrails.evaluate_breakers``, deliberately NOT the
    pipeline entry point: that one calls ``load_guardrails``, which get-or-CREATES,
    and a read-only surface must never seed (the G7 grant would 503 this poll on a
    virgin table). Same four checks, same order, byte-identical reason strings; the
    only thing dropped is the write. Reusing the caller's snapshot also means the
    breach and the state beside it describe ONE instant.

    STATE-BLIND by design (``breached_breaker`` takes no state input): it answers "is
    a breaker breached" even while the brake is already tripped, which is exactly what
    the clear dialog needs to warn that clearing will simply re-trip within the hour.
    DAY KEY: ``latest_run_date(session) or date.today()`` -- the trading day of record
    the digest stamps on ExecutionLog.run_date, the same key every non-screen consult
    resolves (a wall-clock key would count zero of the day's own live orders).
    Costs ZERO queries when no breaker is set (each check is skipped when unset)."""
    hit = guardrails_repo.breached_breaker(
        session, g, run_date=latest_run_date(session) or date.today())
    return None if hit is None else {"breaker": hit[0], "reason": hit[1]}


def _sweep_result(
    *, ran: bool, detail: str = "", entries: list[BrokerOrder] | None = None,
    sells: int = 0, restored: list[str] | None = None,
    unprotected: list[str] | None = None,
) -> dict[str, object]:
    """The HALT response's ``sweep`` block -- one CLOSED shape whatever happened, so
    the panel never type-switches: ``ran`` says whether the venue was actually swept
    (False for a dry-run preview and for the no-broker path), ``detail`` carries the
    reason it did not, and the four lists say what was (or would be) moved."""
    return {
        "ran": ran,
        "detail": detail,
        "cancelled": [{"symbol": o.symbol, "broker_order_id": o.broker_order_id}
                      for o in (entries or [])],
        "sells_kept": sells,
        "stops_restored": restored or [],
        "unprotected": unprotected or [],
    }


def _record_disarm(session: Session, *, reason: str, orders_cancelled: int) -> None:
    """Persist a DisarmEvent so the System Behavior Auditor can see an unexpected
    disarm. Best-effort: a disarm (complete OR partial) has moved venue state, so
    a failure to log it must not change the response -- a completed run's 200
    stays a 200, and a failed run's 503 must carry the ORIGINAL broker error,
    never a masking DB one. The rollback-first clears any failed transaction the
    request may have left (the SQLAlchemyError path arrives here with a poisoned
    session; add+commit on it would always lose the event) -- safe because this
    endpoint only READS before recording, so there is nothing pending to lose."""
    try:
        session.rollback()
        session.add(DisarmEvent(
            created_at=datetime.now(UTC), reason=reason, orders_cancelled=orders_cancelled))
        session.commit()
    except Exception:  # noqa: BLE001 -- audit logging is best-effort; the disarm stands
        log.warning("failed to persist DisarmEvent", exc_info=True)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 -- a dead session must not mask the response either
            log.warning("DisarmEvent rollback also failed", exc_info=True)


def _cfg_row(
    key: str, env: str | None, value: object, note: str = ""
) -> dict[str, object]:
    """One CONFIG panel row: display key, the env var that sets it (None for a
    code constant), the CURRENT resolved value, and a short meaning note."""
    return {"key": key, "env": env, "value": value, "note": note}


def build_safety_router(
    *,
    _session: Callable[[], Iterator[Session]],
    edge_dir: Path | None,
    resolved_broker_factory: Callable[[], BrokerClient | None],
    broker_snapshot: BrokerSnapshot,
    disarm_lock: threading.Lock,
    action_nonce: ActionNonce,
) -> APIRouter:
    """The gate/DISARM/safety endpoints, closed over the app's seams: the session
    dependency, the edge dir, the resolved broker factory (DISARM needs a LIVE
    client), the cached venue snapshot (also parked on ``app.state``), the DISARM
    single-flight lock (also parked on ``app.state`` for tests), and the
    post-action wake nonce (bumped by a REAL disarm run)."""
    router = APIRouter()

    @router.get("/api/gate")
    def gate(session: Session = Depends(_session)) -> dict[str, object]:
        """The advisory autonomy gate + today's analyst spend, as one status object.

        ``ready`` and ``countdown`` come from ``pipeline.autonomy`` VERBATIM -- the
        countdown's line format is pinned by tests/pipeline/test_autonomy_countdown.py,
        so the arithmetic is never reimplemented here. A missing verdicts sidecar
        reads as not-ready (the gate's own missing-file posture), never an error.
        ``execution_mode`` reads the env-backed settings at request time;
        ``analyst_spend_today_usd`` sums ``est_cost_usd`` over TODAY's LLM spend
        UNIONED across analyst calls + Journal v2 coach/audit rows (NULL costs --
        the deterministic/fallback path -- count 0.0).
        ``broker_configured`` is settings TRUTHINESS (is ``SWING_BROKER`` set),
        NEVER connectivity: DISARM's enablement keys on it, and it rides this
        already-polled endpoint so the always-visible masthead needs no extra poll.
        ``brake_state`` rides it for the same reason (the masthead brake chip):
        ``peek_guardrails(session).state`` rendered DIRECTLY -- 'ok' | 'halted' |
        'tripped', never a consult VERDICT, so the chip can never say 'ok' about a
        halted brake. ONE column select on an already-polled endpoint, no venue call
        and NO write: ``peek`` (not ``load``) because a poll must never seed a row --
        under a read-only DB grant that INSERT would 503 the masthead, and an empty
        table honestly reads as the default 'ok'. Unlike ``execution_mode`` this is a
        DB read: the brake row is the venue of record every process shares, so a trip
        landed by an Azure job shows on the next poll (the column select bypasses the
        identity map).
        """
        report = autonomy_gate(session, edge_dir=resolve_edge_dir(edge_dir))
        today = date.today()
        # union across analyst calls + Journal v2 coach/audit spend (NULL costs -> 0.0)
        spend_today = sum(
            (c or 0.0) for d, c in spend_rows_since(session, today) if d == today
        )
        settings = load_settings()
        return {
            "ready": report.ready,
            "countdown": gate_countdown(report),
            "execution_mode": settings.execution_mode,
            "broker_configured": bool(settings.broker),
            "brake_state": guardrails_repo.peek_guardrails(session).state,
            "analyst_spend_today_usd": spend_today,
        }

    def _resume_disarm(
        session: Session, broker: BrokerClient, g: GuardrailsState, *, dry_run: bool,
    ) -> dict[str, object]:
        """DISARM on a TRIPPED book whose sweep never finished: resume THAT sweep.

        Why not the raw cockpit sweep (the 2026-07-18 red team): the trip response
        keys its stop re-submits ``guardrail-{trip_id}``, and a cockpit run carrying
        its own per-second suffix would NOT collide with a concurrent trip sweep at
        the venue -- the duplicate-client_order_id rejection, the only cross-process
        interlock that exists, cannot fire on two different ids. Two live GTC sell
        stops on a margin account close the position and then SHORT it. Routing
        through ``resume_incomplete_sweep`` makes both processes derive the SAME ids
        from the same trip, so the venue collapses the race. It also finishes the
        bookkeeping the cockpit is otherwise unable to finish: the resume records the
        sweep outcome, so a cockpit disarm flips ``sweep_state`` to 'complete' and
        un-sticks the TRIPPED/SWEEP-PARTIAL banner (and the Auditor's stuck-sweep
        rule) instead of leaving it retrying forever.

        The audit trail is the RESUME's: it writes ``DisarmEvent(reason=
        'guardrail:<breaker>')`` itself, which Task 14 grades as SANCTIONED conduct.
        No second 'cockpit' event is written here -- that would double-count one
        sweep AND re-label a correctly-firing brake as an unexplained cockpit disarm.

        Venue failures do NOT 503 here: ``resume_incomplete_sweep`` records them as
        ``sweep_state='partial'`` with a class-name-only detail and returns, so the
        honest report is a 200 whose ``sweep_state`` says 'partial' -- visible,
        re-runnable, and identical to what every other process would have reported.

        ``dry_run`` previews: ``resume_incomplete_sweep`` has no dry-run mode (it is
        the response protocol, not a query), so the preview composes the two disarm
        helpers directly with ``dry_run=True`` under the key the real resume WOULD
        use. That is the only path here with itemized arrays; a REAL resume cannot
        itemize (the pipeline returns a bool), so its counts ride ``detail`` --
        the sweep event's own recorded summary, verbatim -- and the four arrays stay
        empty. The full row is in the guardrails event history either way.
        """
        trip_id = g.trip_id
        assert trip_id is not None  # the caller gates on this
        key = f"guardrail-{trip_id}"
        if dry_run:
            entries, sells = pull_entry_orders(broker, dry_run=True)
            restored, unprotected = ensure_stop_protection(
                broker, lambda sym: latest_recorded_stop(session, sym),
                key_suffix=key, dry_run=True)
            return {
                "dry_run": True,
                "mode": "guardrail-resume-preview",
                "trip_id": trip_id,
                "sweep_state": g.sweep_state,
                "detail": f"would resume the sweep for trip {trip_id} "
                          f"(client_order_id key {key!r})",
                "cancelled": [{"symbol": o.symbol,
                               "broker_order_id": o.broker_order_id}
                              for o in entries],
                "sells_kept": len(sells),
                "stops_restored": restored,
                "unprotected": unprotected,
            }
        try:
            ran = gpipe.resume_incomplete_sweep(
                session, broker=broker, source="cockpit")
        finally:
            # The resume swallows venue errors, but it can still have moved venue
            # state before one -- same posture as the raw path: invalidate FIRST
            # (a woken fetch must never hit the stale cache), then wake.
            broker_snapshot.invalidate()
            action_nonce.bump()
        after = guardrails_repo.peek_guardrails(session)
        return {
            "dry_run": False,
            "mode": "guardrail-resume",
            "trip_id": trip_id,
            "sweep_state": after.sweep_state,
            "detail": (_sweep_detail(session) if ran else
                       "nothing to resume -- another process finished this "
                       "trip's sweep first"),
            # Not itemized on this path (the pipeline returns a bool, not the
            # orders): ``detail`` carries the sweep's own recorded summary, and the
            # guardrails event history has the row. Kept as empty lists rather than
            # dropped so the wire shape stays a superset of a plain disarm.
            "cancelled": [],
            "sells_kept": 0,
            "stops_restored": [],
            "unprotected": [],
        }

    @router.post("/api/disarm", dependencies=[Depends(_require_cockpit)])
    def disarm_book(
        dry_run: bool = Query(default=False),
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """The DISARM runbook step over HTTP: pull entry-side orders, keep the stops.

        Semantics are ``pipeline.disarm``'s, exactly: ONLY ``side == 'buy'`` open
        orders are cancelled (a blanket cancel would strip the bracket stop legs off
        the very positions disarm deliberately does NOT close -- the 2026-07-04
        bug); every remaining position must end stop-protected, a dead stop leg
        re-submitted as a plain GTC stop at the ExecutionLog ticket's RECORDED
        level (COPIED, never computed -- North Star #4); no recorded level -> the
        position is named in ``unprotected`` and LEFT ALONE (never auto-closed --
        North Star #3). Header-guarded (``_require_cockpit``). SINGLE-FLIGHT:
        two concurrent real runs could both read the open-order list before
        either cancels, and the per-second ``key_suffix`` only collapses stop
        re-submits landing within the same second -- overlapping runs risk
        DUPLICATE live GTC sell stops (2x the position: triggered, the second
        sells short). The lock is acquired NON-blocking; the loser answers 409
        'disarm already in flight' without touching the venue. The broker comes
        from the FACTORY seam, live per request -- the cached snapshot is a READ
        and nothing to cancel through. A None factory answer (no broker
        configured) is a 409 STATE, not a crash; a broker/factory error is a 503
        carrying the exception CLASS only (leak posture). ``dry_run=1`` previews
        -- ``cancelled`` / ``stops_restored`` say what a real run WOULD do, the
        venue is untouched. After a REAL run the broker snapshot cache is
        invalidated, the post-action nonce bumps, AND a DisarmEvent is persisted
        for the System Behavior Auditor (all three in ``finally`` -- a partial
        disarm has still moved venue state), so the UI never renders pre-disarm
        orders for up to a TTL, other windows wake immediately, and the Auditor's
        breach scan sees the attempt even when the run 503s partway (reason
        ``cockpit`` on completion, ``cockpit-partial`` on the failure path).

        TRIP-AWARE (Task 15, the Task-6 red-team closure): when the brake is
        ``tripped`` with an unfinished sweep, this endpoint does NOT run its own
        sweep -- it routes through ``gpipe.resume_incomplete_sweep`` instead (see
        ``_resume_disarm``). Everything else -- 'ok', 'halted', and a tripped book
        whose sweep already reads 'complete' -- takes the body below UNCHANGED; the
        routing costs one column select (``peek``: a read, never a seed) taken
        AFTER the lock and the broker, so no ordering or status code moves.
        """
        if not disarm_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="disarm already in flight")
        try:
            try:
                broker = resolved_broker_factory()
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail=broker_error_detail(exc)
                ) from exc
            if broker is None:
                raise HTTPException(status_code=409, detail="no broker configured")
            g = guardrails_repo.peek_guardrails(session)
            # ``gpipe._INCOMPLETE_SWEEPS`` rather than a literal: ONE definition of
            # "the sweep has not finished" across the pipeline and the cockpit.
            # ``trip_id is None`` on a tripped row is never expected -- if it ever
            # happens the resume could not key a sweep outcome anyway, so fall
            # through to the raw sweep (protection now beats bookkeeping).
            if (g.state == "tripped" and g.sweep_state in gpipe._INCOMPLETE_SWEEPS
                    and g.trip_id is not None):
                return _resume_disarm(session, broker, g, dry_run=dry_run)
            # Pre-bound so the FAILURE path can report how many entries were pulled
            # before the raise: pull_entry_orders raising mid-cancel leaves the name
            # unbound, and 0 ("we don't know that any cancel landed") is the honest
            # floor -- never an invented count.
            entries: list[BrokerOrder] = []
            completed = False
            try:
                entries, sells = pull_entry_orders(broker, dry_run=dry_run)
                restored, unprotected = ensure_stop_protection(
                    broker, lambda sym: latest_recorded_stop(session, sym),
                    key_suffix=f"cockpit-{datetime.now(UTC):%Y%m%d%H%M%S}",
                    dry_run=dry_run)
                completed = True
            except SQLAlchemyError:
                raise  # the app-level handler's 503: a DB failure is not a broker error
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail=broker_error_detail(exc)
                ) from exc
            finally:
                if not dry_run:
                    # A PARTIAL disarm has still moved venue state, so ALL THREE run
                    # on the failure path too: invalidate FIRST (a woken fetch must
                    # never hit the stale cache), then the post-action wake -- the
                    # venue calls that returned before a raise are durable, and
                    # other windows must refetch NOW, not at the 60s poll floor;
                    # staleness is scariest on exactly this action. A dry run does
                    # none of them: it changed nothing, and Task 15's hold-to-confirm
                    # fires a preview on EVERY hold-start -- waking all windows
                    # per hold would be noise.
                    broker_snapshot.invalidate()
                    action_nonce.bump()
                    # Every REAL attempt is recorded for the Auditor -- the breach
                    # scan exists to flag disarms, and a disarm that cancelled the
                    # entries then died restoring stops has moved MORE alarming
                    # venue state than a clean one, not less. 'cockpit-partial'
                    # names the failure path; best-effort (_record_disarm never
                    # raises), so a failed event write cannot mask the 503.
                    _record_disarm(
                        session,
                        reason="cockpit" if completed else "cockpit-partial",
                        orders_cancelled=len(entries))
            return {
                "dry_run": dry_run,
                "cancelled": [{"symbol": o.symbol,
                               "broker_order_id": o.broker_order_id}
                              for o in entries],
                "sells_kept": len(sells),
                "stops_restored": restored,
                "unprotected": unprotected,
            }
        finally:
            disarm_lock.release()

    @router.get("/api/execution/safety")
    def execution_safety(session: Session = Depends(_session)) -> dict[str, object]:
        """The Execution Safety screen in one read: is real money possible, and why not.

        ``broker_configured`` is settings TRUTHINESS, never connectivity (same rule
        as ``/api/gate``). ``preflight`` is ``pipeline.preflight``'s report over a
        FRESH client from the factory seam; a None factory answer takes the report's
        None-broker shape (the config line evaluated from settings for REAL, explicit
        not-applicable broker lines -- the default local setup is never a 500), and a
        RAISING factory degrades to that same shape -- the config line stays honest
        (the settings ARE configured; the factory failed) and the reachable line
        carries the exception CLASS only (leak posture).
        ``locks`` renders ``can_arm_real_money``'s three components
        individually (``gate_ready`` reuses the report's advisory gate line -- one
        evaluation per request); ``caps_mandate`` is ``real_money_limits_ok`` over
        the resolved limits. ``guardrails`` is the brake: the mandate verdict +
        reason (the same definition execution enforces at submit time) beside the
        RAW ``state`` / ``sweep_state``, rendered DIRECTLY so the screen can never
        report 'ok' about a halted brake. All four fields come from ONE
        ``peek_guardrails`` snapshot -- a second read could land either side of a
        trip and publish a verdict that contradicts the state beside it -- and
        ``peek`` never seeds, so this poll cannot write (a read-only DB grant must
        not 503 the safety screen).
        ``env_scope`` is the honesty label, and it covers the ENV-DERIVED fields
        only (``mode``, ``locks``, ``caps_mandate``, ``broker_configured``, and the
        preflight block's ``config`` / ``reachable`` / ``funded`` / ``caps`` rows):
        those read THIS process's env -- the Azure jobs run under their own.
        ``guardrails`` is NOT one of them -- it reads the shared ``agent_guardrails``
        row, the brake's single venue of record for EVERY process, so a trip landed
        by an Azure job shows here.
        The top-level ``guardrails`` entry and the preflight block's ``guardrails``
        ROW are computed from SEPARATE snapshots (preflight peeks for itself), so a
        trip landing mid-request can leave them momentarily disagreeing -- the next
        poll converges. Deliberate: threading a snapshot through ``preflight`` would
        widen its signature for a sub-second cosmetic win.
        ``bracket_shield`` reads the CACHED broker snapshot (the venue-truth table:
        see ``_bracket_shield`` -- UNKNOWN is never rendered green). Every DB read
        here is a cheap column select: this endpoint already makes a REAL broker call
        through the factory, and nothing added since touches the venue.
        """
        settings = load_settings()
        broker: BrokerClient | None
        broker_error: str | None = None
        try:
            broker = resolved_broker_factory()
        except Exception as exc:
            broker = None
            broker_error = broker_error_detail(exc)
        report = preflight(session, settings, broker=broker,
                           edge_dir=resolve_edge_dir(edge_dir))
        if broker_error is not None:
            report = PreflightReport(go=report.go, checks=[
                replace(c, detail=broker_error) if c.name == "reachable" else c
                for c in report.checks])
        # Default False: a renamed/absent gate check degrades to not-green
        # (UNKNOWN is never green), instead of a StopIteration 500.
        gate_ready = next(
            (c.ok for c in report.checks if c.name == "autonomy_gate"), False)
        mode, limits = resolve_execution(settings)
        caps_ok, caps_reason = real_money_limits_ok(limits)
        # ONE snapshot for all four brake fields: the verdict (may real money
        # dispatch) and the raw state/sweep bookkeeping must describe the SAME
        # instant, or a trip landing between two reads would publish 'ok' beside
        # 'tripped'. peek (not load) so the poll stays read-only; the column select
        # bypasses the identity map, so another process's trip is visible at once.
        g = guardrails_repo.peek_guardrails(session)
        g_ok, g_reason = guardrails_repo.mandate_from_state(g)
        return {
            "broker_configured": bool(settings.broker),
            "mode": mode,
            "env_scope": "this process — the Azure jobs run under their own env",
            "locks": {
                "mode_is_live": mode == "live",
                "allow_real_money": settings.allow_real_money,
                "gate_ready": gate_ready,
            },
            "caps_mandate": {"ok": caps_ok, "reason": caps_reason},
            "guardrails": {"ok": g_ok, "reason": g_reason, "state": g.state,
                           "sweep_state": g.sweep_state},
            "preflight": {
                "go": report.go,
                "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail,
                            "critical": c.critical} for c in report.checks],
            },
            "bracket_shield": _bracket_shield(session, broker_snapshot.get()),
        }

    @router.get("/api/guardrails")
    def guardrails(session: Session = Depends(_session)) -> dict[str, object]:
        """The brake, whole: state + limits, the live breach, and recent history.

        DB-ONLY and CHEAP -- this poll must never ride the venue (the safety report
        already pays for one broker call per request; the brake panel next to it must
        not double that), and it must never WRITE: ``peek_guardrails``, never the
        seeding ``load``. These endpoints are the FIRST cockpit writers of
        ``agent_guardrails``, so the read half stays provably clean -- a read-only DB
        grant (G7 not yet issued in prod) must still get a 200 with the honest default
        rather than a 503 from an INSERT nobody asked for.

        ``current_breach`` is ``{breaker, reason}`` or null: a STATE-BLIND evaluation
        of the same four breakers execution enforces (see ``_current_breach``), taken
        from the SAME snapshot as the state beside it. It is what powers the clear
        dialog's warning -- clearing a trip whose breach is still real just re-trips
        on the next hourly consult, by design, and the operator has to be told that
        BEFORE they clear rather than by the second trip email.

        ``events`` is the last 25 ``agent_guardrail_events`` NEWEST FIRST -- the
        append-only audit trail every write below lands in (edit / halt / trip /
        clear / sweep), rendered as a column select (no ORM entity to go stale, no
        ``values_json`` on the wire: the diff blob is history-panel material, not
        something the brake status needs).
        """
        g = guardrails_repo.peek_guardrails(session)
        rows = session.execute(
            select(AgentGuardrailEvent.id, AgentGuardrailEvent.created_at,
                   AgentGuardrailEvent.kind, AgentGuardrailEvent.breaker,
                   AgentGuardrailEvent.reason, AgentGuardrailEvent.source)
            .order_by(AgentGuardrailEvent.id.desc())
            .limit(_EVENT_HISTORY)
        ).all()
        return _state_body(g) | {
            "current_breach": _current_breach(session, g),
            "events": [
                {"id": r.id, "created_at": _utc_iso(r.created_at), "kind": r.kind,
                 "breaker": r.breaker, "reason": r.reason, "source": r.source}
                for r in rows
            ],
        }

    @router.post("/api/guardrails", dependencies=[Depends(_require_cockpit)])
    def guardrails_action(
        body: GuardrailAction,
        dry_run: bool = Query(default=False),
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """The brake's four operator actions. Header-guarded like every mutation.

        Every one of them routes to a ``guardrails_repo`` state-machine function --
        those SEED (correctly: they are writes, and a conditional UPDATE needs a
        target row), audit, and commit the state change together with its event, so
        a crash can never release a brake without leaving its record. Nothing here
        re-implements a transition, and nothing wraps one in a swallowing
        try/except: a ``SQLAlchemyError`` on the primary state write PROPAGATES to
        the app-level 503 handler. That is the hard rule of this endpoint -- a 200
        that says the brake moved when the UPDATE was refused is the one failure a
        brake may not have (the ``_record_disarm`` best-effort posture applies ONLY
        to audit rows written AFTER a change already committed).

        * ``edit`` -- the six limit columns (``guardrails_repo``'s whitelist is the
          gate; unknown key / non-positive breaker -> 422 wearing the repo's own
          message). Presence decides: an omitted key is untouched, an explicit null
          unsets. State columns can never ride an edit, so editing a cap while
          tripped leaves the brake tripped.
        * ``halt`` -- 'ok' -> 'halted' AND the protective sweep, so it takes the
          SAME single-flight ``disarm_lock`` /api/disarm holds (409 to the loser,
          before the factory resolves). The DB brake lands FIRST -- persist-first,
          exactly like ``respond_to_trip``: the halt blocks every dispatch path on
          DB truth alone, and the venue sweep is best-effort on top (no broker ->
          still 200, ``sweep.ran`` False). ``dry_run=1`` previews and changes
          NOTHING -- not the state, not a row, not the venue.
        * ``clear_halt`` -- 'halted' -> 'ok', DB-only. A trip never clears here.
        * ``clear_trip`` -- requires ``ack_trip_id``: the clear only matches the trip
          the operator actually READ, so a stale cockpit screen cannot release a
          newer trip. The response carries ``still_breached`` so the panel can say
          immediately that this will re-trip within the hour.
        """
        if body.action == "edit":
            fields = {k: getattr(body, k)
                      for k in sorted(body.model_fields_set - _NON_LIMIT_KEYS)}
            try:
                guardrails_repo.edit_limits(session, source="cockpit", **fields)
            except ValueError as exc:
                # The repo's message, verbatim: it names the offending column and the
                # rule it broke, which is more useful than anything restated here.
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            action_nonce.bump()
            g = guardrails_repo.peek_guardrails(session)
            return _state_body(g) | {"action": "edit",
                                     "current_breach": _current_breach(session, g)}

        if body.action == "halt":
            return _halt_action(session, dry_run=dry_run)

        if body.action == "clear_halt":
            if not guardrails_repo.clear_halt(session, source="cockpit"):
                state = guardrails_repo.peek_guardrails(session).state
                raise HTTPException(
                    status_code=409,
                    detail=f"state is {state} -- there is no HALT to clear")
            action_nonce.bump()
            return _state_body(guardrails_repo.peek_guardrails(session)) | {
                "action": "clear_halt"}

        if body.ack_trip_id is None:
            raise HTTPException(status_code=422,
                                detail="clear_trip requires ack_trip_id")
        if not guardrails_repo.clear(session, acknowledged_trip_id=body.ack_trip_id,
                                     source="cockpit"):
            raise HTTPException(
                status_code=409, detail="trip id is stale or state is not tripped")
        action_nonce.bump()
        g = guardrails_repo.peek_guardrails(session)
        return _state_body(g) | {"action": "clear_trip",
                                 "still_breached": _current_breach(session, g)}

    def _halt_action(session: Session, *, dry_run: bool) -> dict[str, object]:
        """The HALT branch: single-flight, persist-first, sweep best-effort.

        Lock coverage is unconditional (the dry run makes read-only venue calls, and
        the raw DISARM endpoint locks its previews too), and it is released in the
        outer ``finally`` so a failed halt never wedges the endpoint shut.

        ORDER, and why: lock -> the DB halt -> the broker -> the sweep. The brake is
        DB truth -- every dispatch path consults the row -- so the halt has to be
        durable before anything slower is attempted: a venue that is down, absent, or
        whose credentials no longer resolve must NEVER be able to stop an operator
        from stopping the machine (resolving the client first would 503 the request
        with the brake still off). The sweep is the same body the trip response and
        the kill switch run, keyed ``halt-cockpit-<ts>``, and it journals a
        ``DisarmEvent(reason='halt')`` -- the same reason the digest's manual-HALT
        sweep writes, so the Auditor grades both as expected conduct rather than an
        unexplained disarm.

        Once the brake is on, a broker that cannot be resolved and a sweep that dies
        mid-flight answer the SAME way: 503 carrying the exception CLASS only (leak
        posture), never a 200 with a quiet note -- a UI reads 200 as "done", and
        "your resting entry orders are still working" is exactly what the operator
        must not miss. The halt itself is already durable: the next poll shows
        'halted', and pressing HALT again answers 409 naming that state, so the 503
        can never be mistaken for a brake that failed to engage.

        A DRY RUN resolves the broker first and never transitions anything -- there
        is no brake to protect, and a preview that cannot reach the venue has nothing
        to say.
        """
        if not disarm_lock.acquire(blocking=False):
            raise HTTPException(status_code=409,
                                detail="guardrail action already in flight")
        # Pre-bound so the real run's FAILURE path can report how many entries were
        # pulled before the raise: ``pull_entry_orders`` dying mid-cancel leaves the
        # name unbound, and 0 ("we don't know that any cancel landed") is the honest
        # floor -- never an invented count. Same reasoning as /api/disarm's.
        entries: list[BrokerOrder] = []
        try:
            if dry_run:
                # Preview only: peek (no seed), no transition, and the venue read
                # through the two disarm helpers' dry-run mode. Task 16 fires this
                # on every hold-START, so it must stay free of side effects.
                try:
                    broker = resolved_broker_factory()
                except Exception as exc:
                    raise HTTPException(
                        status_code=503, detail=broker_error_detail(exc)) from exc
                g = guardrails_repo.peek_guardrails(session)
                if broker is None:
                    sweep = _sweep_result(ran=False, detail=_NO_BROKER)
                else:
                    entries, sells = pull_entry_orders(broker, dry_run=True)
                    restored, unprotected = ensure_stop_protection(
                        broker, lambda sym: latest_recorded_stop(session, sym),
                        key_suffix=_halt_key(), dry_run=True)
                    sweep = _sweep_result(ran=False, entries=entries,
                                          sells=len(sells), restored=restored,
                                          unprotected=unprotected)
                return _state_body(g) | {"action": "halt", "dry_run": True,
                                         "sweep": sweep}

            halted = False
            venue_touched = False
            try:
                # The PRIMARY state write, and it goes FIRST: a SQLAlchemyError here
                # rides the app-level 503 handler -- deliberately NOT caught.
                if not guardrails_repo.halt(session, source="cockpit"):
                    state = guardrails_repo.peek_guardrails(session).state
                    raise HTTPException(
                        status_code=409,
                        detail=f"state is not ok -- nothing to halt "
                               f"(state is {state})")
                halted = True
                try:
                    broker = resolved_broker_factory()
                except Exception as exc:
                    raise HTTPException(
                        status_code=503, detail=broker_error_detail(exc)) from exc
                if broker is None:
                    # The brake is DB truth and it HOLDS; the venue sweep is
                    # best-effort here exactly as respond_to_trip's broker-None
                    # path is (that one defers to the next cycle's resume).
                    sweep = _sweep_result(ran=False, detail=_NO_BROKER)
                else:
                    venue_touched = True
                    try:
                        entries, sells = pull_entry_orders(broker)
                        restored, unprotected = ensure_stop_protection(
                            broker, lambda sym: latest_recorded_stop(session, sym),
                            key_suffix=_halt_key())
                    except SQLAlchemyError:
                        raise  # a DB failure is not a broker error
                    except Exception as exc:
                        raise HTTPException(
                            status_code=503, detail=broker_error_detail(exc)) from exc
                    sweep = _sweep_result(ran=True, entries=entries, sells=len(sells),
                                          restored=restored, unprotected=unprotected)
            finally:
                # A PARTIAL sweep has still moved venue state, so these run on the
                # failure path too: invalidate FIRST (a woken fetch must never hit
                # the stale cache), then wake every window -- the masthead brake
                # chip has just changed -- then journal the venue-moving sweep for
                # the Auditor (best-effort; it never raises, so it cannot mask a
                # 503, and the halt has already committed so its rollback-first
                # loses nothing).
                if venue_touched:
                    broker_snapshot.invalidate()
                if halted:
                    action_nonce.bump()
                    if venue_touched:
                        gpipe.record_disarm_event(
                            session, reason="halt", orders_cancelled=len(entries))
            return _state_body(guardrails_repo.peek_guardrails(session)) | {
                "action": "halt", "dry_run": False, "sweep": sweep}
        finally:
            disarm_lock.release()

    @router.get("/api/config")
    def config() -> dict[str, object]:
        """The live configuration, READ-ONLY BY DESIGN -- the instrument panel
        shows every knob; git remains the control column (North Star #1/#3: no
        config write ever originates in the cockpit, and env is per-process, so
        a cockpit 'edit' could not reach the Azure jobs anyway). Three sections,
        each row ``{key, env, value, note}`` with a per-section ``change_via``
        stating exactly where the real edit lives. Leak posture: secrets are
        never echoed (the one secret-adjacent value here is the broker's
        real-money switch, a boolean); the DB URL is deliberately absent (the
        masthead chip already carries its safe label). ``env_scope`` mirrors
        the safety report's honesty label: these are THIS process's values --
        the Azure jobs run under their own env (infra/main.bicepparam)."""
        s = load_settings()
        risk_unit, max_shares = resolve_risk_unit(s)
        mode, limits = resolve_execution(s)
        strategy = StrategyConfig()
        gex = GexConfig()
        sizing = [
            _cfg_row("account equity", "SWING_ACCOUNT_EQUITY", s.account_equity,
                     "dollars; 1R = equity × risk pct"),
            _cfg_row("risk pct", "SWING_RISK_PCT", s.risk_pct,
                     "fraction of equity risked per trade (default 0.01)"),
            _cfg_row("risk per trade $", "SWING_RISK_PER_TRADE_DOLLARS",
                     s.risk_per_trade_dollars,
                     "explicit 1R override — wins over equity × pct when set"),
            _cfg_row("resolved 1R", None, risk_unit,
                     "the computed risk unit; 0 = sizing unconfigured (R-multiples only)"),
            _cfg_row("max shares", "SWING_MAX_SHARES", max_shares,
                     "hard share cap per order (null = uncapped)"),
        ]
        execution = [
            _cfg_row("execution mode", "SWING_EXECUTION_MODE", mode,
                     "off | manual | paper | live — unknown coerces to off (fail-safe)"),
            _cfg_row("broker", "SWING_BROKER", s.broker or None,
                     "venue for brackets/DISARM; unset = lamps UNKNOWN, DISARM disabled"),
            _cfg_row("execute play types", "SWING_EXECUTE_PLAY_TYPES",
                     sorted(s.execute_play_types)
                     if s.execute_play_types is not None else None,
                     "execution ceiling: unset = all play types; garbage members "
                     "are dropped (fail-closed); empty = nothing dispatches"),
            _cfg_row("allow real money", "SWING_BROKER_ALLOW_REAL_MONEY",
                     s.allow_real_money,
                     "one of the three live locks — false blocks live arming"),
            _cfg_row("max daily notional $", "SWING_MAX_DAILY_NOTIONAL",
                     limits.max_daily_notional,
                     "per-account-day recorded order notional (null = unbounded)"),
            _cfg_row("max daily loss (R)", "SWING_MAX_DAILY_LOSS",
                     limits.max_daily_loss,
                     "R threshold: new orders blocked once the day sums ≤ −this"),
            _cfg_row("max concurrent", "SWING_MAX_CONCURRENT", limits.max_concurrent,
                     "open positions per account (null = unbounded)"),
        ]
        analyst = [
            _cfg_row("deep analysis", "SWING_DEEP_ANALYSIS", s.deep_analysis_enabled,
                     "the Opus web-search analyst (default off)"),
            _cfg_row("analysis model", "SWING_ANALYSIS_MODEL", s.analysis_model, ""),
            _cfg_row("deep analysis max $/run", "SWING_DEEP_ANALYSIS_MAX_USD",
                     s.deep_analysis_max_usd, "per-run spend ceiling (null = uncapped)"),
            _cfg_row("coach", "SWING_COACH_ENABLED", s.coach_enabled,
                     "Personal Trade Coach (Journal)"),
            _cfg_row("auditor", "SWING_AUDIT_ENABLED", s.audit_enabled,
                     "System Behavior Auditor (System Audit)"),
        ]
        return {
            "env_scope": "this process — the Azure jobs run under their own env",
            "sections": [
                {
                    "title": "sizing",
                    "change_via": "user-level env on this box · infra/main.bicepparam in prod (bicep deploy applies)",
                    "rows": sizing,
                },
                {
                    "title": "execution",
                    "change_via": "user-level env on this box · infra/main.bicepparam in prod (bicep deploy applies)",
                    "rows": execution,
                },
                {
                    "title": "analyst & coaches",
                    "change_via": "user-level env on this box · infra/main.bicepparam or jobs.bicep in prod",
                    "rows": analyst,
                },
                {
                    "title": "engine (StrategyConfig — the incumbent)",
                    "change_via": "config.py via PR — the optimizer proposes, evidence gates, a human merges (North Star #1)",
                    "rows": [
                        _cfg_row(k, None, v, "")
                        for k, v in asdict(strategy).items()
                    ],
                },
                {
                    "title": "GEX lab (GexConfig)",
                    "change_via": "options/config.py via PR — lab knobs never live in the equity config",
                    "rows": [
                        _cfg_row(
                            k, None,
                            list(v) if isinstance(v, tuple) else v, "",
                        )
                        for k, v in asdict(gex).items()
                    ],
                },
            ],
        }

    return router


def _bracket_shield(session: Session, snapshot: Snapshot | None) -> dict[str, object]:
    """The safety screen's venue-truth table: one row per VENUE position, each with
    its ``_bracket`` state -- ``armed`` (a live protective sell stop at the venue),
    ``db-only`` (only a recorded ExecutionLog level -- ``latest_recorded_stop``,
    the same lookup disarm restores from), or ``unprotected`` (no level anywhere,
    loud). No snapshot (no broker configured, or the venue read failed/degraded)
    -> ``known: False`` with an empty table: absence of evidence is never a claim,
    so UNKNOWN can never render green."""
    if snapshot is None:
        return {"known": False, "as_of": None, "positions": []}
    armed = _armed_symbols(snapshot)
    return {
        "known": True,
        "as_of": snapshot.as_of.isoformat(),
        "positions": [
            {"symbol": pos.symbol, "qty": pos.qty,
             "state": _bracket(pos.symbol, latest_recorded_stop(session, pos.symbol),
                               snapshot=snapshot, armed=armed)}
            for pos in snapshot.positions
        ],
    }

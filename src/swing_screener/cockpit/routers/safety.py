"""The execution-safety surface: the advisory gate, the DISARM runbook action, the
Execution Safety report, and (Task 15) the guardrails brake's own read/write
endpoints. Moved verbatim out of ``cockpit/api.py``; lock semantics (single-flight,
non-blocking acquire, release in the outer ``finally``) and every status code are
unchanged."""

import json
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
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
#:
#: ``disabled`` (set_scope's body key) is DELIBERATELY not listed: adding it would
#: make ``{"action": "edit", "disabled": [...]}`` a silently-ignored field, and
#: scope is its own verb. Leaving it out routes it into ``edit_limits``, which
#: refuses it BY NAME -- the loud outcome a mixed body deserves.
_NON_LIMIT_KEYS = frozenset({"action", "ack_trip_id"})

#: How much guardrail history the panel gets in one read. The table is append-only and
#: low-volume (one row per operator action / trip / sweep), so 25 covers "what just
#: happened" without paging machinery the brake does not need.
_EVENT_HISTORY = 25

#: The no-broker wording, shared with /api/disarm's 409 so the two surfaces can never
#: describe the same configuration state in two different ways.
_NO_BROKER = "no broker configured"

#: The single-flight 409, shared by DISARM and HALT. They contend for the SAME
#: ``disarm_lock`` (both submit protective stops), so they must not describe that
#: contention in two different ways -- an operator who reads "disarm already in
#: flight" after pressing HALT would go looking for a disarm nobody ran.
_ACTION_IN_FLIGHT = "a protective action is already in flight"

#: The three 503 detail PREFIXES this router emits, and the ONLY thing a client may
#: branch on. They are a stable contract (Task 16 discriminates failure kinds with
#: them; the text after the prefix is human-facing and may change):
#:
#: * ``database error (`` -- the app-level SQLAlchemyError handler (cockpit/api.py).
#: * ``broker error (``   -- a venue/factory failure, exception CLASS only.
#: * ``guardrail sweep did not complete`` -- a resumed trip sweep that ended
#:   partial; the venue may still hold working entry orders.
_D503_DB = "database error ("
_D503_BROKER = "broker error ("
_D503_SWEEP = "guardrail sweep did not complete"


def _db_error_detail(exc: Exception) -> str:
    """``database error (ClassName)`` -- the app-level handler's wording, reproduced
    for the one place that must CATCH a DB failure instead of letting it 503: the
    post-commit enrichment reads (see ``_after_write``). Same leak posture (class
    name only) and the same prefix, so a client cannot tell the two apart -- it is
    the same kind of failure, just one that arrived too late to change the outcome."""
    return f"{_D503_DB}{type(exc).__name__})"


#: The ``guardrails_repo`` function-name prefixes stripped off a rejection before it
#: reaches an operator. Listed, not regex-guessed: only the two write verbs this
#: endpoint calls, so an unexpected exception's text is never silently trimmed.
_REPO_PREFIXES = ("edit_limits: ", "set_disabled_play_types: ")


def _client_error(exc: Exception) -> str:
    """A ``guardrails_repo`` rejection as an operator-facing 422 message.

    The repo raises with a ``<function>: `` prefix naming itself -- true but
    internal, and the cockpit is not where a user learns the callee's name. The
    SUBSTANCE (which column, which rule, which invalid play type) is kept verbatim:
    it is the same text the repo's own tests pin, and re-wording it would let the two
    drift."""
    text = str(exc)
    for prefix in _REPO_PREFIXES:
        text = text.removeprefix(prefix)
    return text


def _halt_key() -> str:
    """The HALT sweep's ``key_suffix``: ``halt-cockpit-<UTC to the second>``.

    Mirrors the digest's manual-HALT sweep (``halt-{run_date}``) with the cockpit's
    per-second stamp, so re-submits inside the same second collapse at the venue.
    Cross-process collapse is NOT the goal here -- that is what the trip's
    ``guardrail-{trip_id}`` key is for, and why /api/disarm routes a tripped book
    through the resume instead of a fresh suffix."""
    return f"halt-cockpit-{datetime.now(UTC):%Y%m%d%H%M%S}"


def _rollback_quietly(session: Session, *, what: str) -> None:
    """Discard a swallowed failure's transaction before the caller carries on.

    DEFENCE IN DEPTH, deliberately. In SQLAlchemy 2.0 a failed SELECT does NOT by
    itself deactivate the Session -- only a failed FLUSH does, which is why the raw
    disarm sweep keeps working after a refused brake read even without this call.
    But "which failures deactivate" is a property of the backend and the error
    (a lost connection, a driver that fails mid-transaction, pyodbc against Azure
    SQL), not something the emergency path should have to be right about: every
    later query on a deactivated Session answers ``PendingRollbackError``, itself a
    ``SQLAlchemyError``, which would 503 a DISARM over a table the operator never
    asked about. One cheap call removes the whole question.

    Never raises -- a session too dead to roll back is logged and left alone (the
    same posture ``_record_disarm`` takes)."""
    try:
        session.rollback()
    except Exception:  # noqa: BLE001 -- a dead session must not become the response
        log.warning("rollback after %s also failed", what, exc_info=True)


def _event_watermark(session: Session) -> int:
    """``max(agent_guardrail_events.id)`` right now, or 0 on an empty table.

    Taken BEFORE a sweep so ``_sweep_record`` can prove the row it reports was
    written by THIS request -- see there for why a bare "newest sweep event" is
    not good enough."""
    return int(session.scalar(select(func.max(AgentGuardrailEvent.id))) or 0)


def _sweep_record(session: Session, *, after: int) -> tuple[str, list[str]]:
    """This request's ``sweep`` event: its detail VERBATIM (or an honest admission)
    and the STRUCTURED ``unprotected`` list ``record_sweep_outcome`` wrote beside it.

    The resume path cannot itemize what it moved (``resume_incomplete_sweep``
    returns a bool), but the sweep it just ran recorded its own summary -- "swept: N
    entry order(s) cancelled, M protective stop(s) restored", or a class-name-only
    error on a partial -- as the event's reason. Echoing that string is strictly
    better than restating it: it is the SAME text the Auditor and the event history
    show, so the three can never disagree.

    ``after`` is the id watermark taken before the sweep, and it is load-bearing.
    ``_record_outcome_guarded`` SWALLOWS a failed outcome write (by design -- the
    venue already moved, and a poisoned session would kill the caller), so "no row
    was written" is a real outcome. Without the watermark this would then report a
    PRIOR sweep's text -- an old success narrating a run that just failed, which is
    the worst possible lie on this endpoint. Scoped, that case falls through to a
    detail that says exactly what is known: the sweep ran, its bookkeeping did not
    land, and the state may lag.

    ``unprotected`` is read from the event's ``values_json`` -- the STRUCTURED fact
    ``record_sweep_outcome`` stores, never parsed back out of the detail sentence --
    so the resume carries the same field the raw sweep returns and the panel can
    apply ONE alarm rule across every disarm mode. An absent/garbage blob answers
    ``[]``, which is safe HERE specifically: the only path that renders this to a
    client is the 200, and a failed outcome write leaves ``sweep_state`` incomplete
    and 503s first -- so the empty list never stands in for "unknown"."""
    row = session.execute(
        select(AgentGuardrailEvent.reason, AgentGuardrailEvent.values_json)
        .where(AgentGuardrailEvent.kind == "sweep", AgentGuardrailEvent.id > after)
        .order_by(AgentGuardrailEvent.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return "sweep ran; outcome write failed — state may lag", []
    try:
        blob = json.loads(row.values_json or "{}")
        raw = blob.get("unprotected") if isinstance(blob, dict) else None
    except ValueError:  # a malformed blob must never 500 the emergency path
        raw = None
    return (row.reason or "sweep ran; outcome write failed — state may lag",
            [str(s) for s in raw] if isinstance(raw, list) else [])


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

    action: Literal["edit", "halt", "clear_halt", "clear_trip", "set_scope"]
    #: clear_trip only: the trip id the operator actually acknowledged.
    ack_trip_id: int | None = None
    #: set_scope only: the WHOLE new disabled set (``[]`` re-enables everything the
    #: env ceiling still allows). Declared so the wire gets real coercion, and
    #: DELIBERATELY not in ``_NON_LIMIT_KEYS``: sending it on an ``edit`` reaches
    #: ``edit_limits``' whitelist and comes back as its own 422 naming ``disabled``
    #: -- scope is its own verb, and a body that mixes the two is a client bug worth
    #: saying out loud rather than half-applying.
    disabled: list[str] | None = None
    max_daily_loss_usd: float | None = Field(default=None, allow_inf_nan=False)
    max_trades_per_day: int | None = None
    max_drawdown_usd: float | None = Field(default=None, allow_inf_nan=False)
    loss_streak_halt: int | None = None
    hwm_anchor_date: date | None = None
    hwm_baseline_usd: float = Field(default=0.0, allow_inf_nan=False)


def _state_body(g: GuardrailsState) -> dict[str, object]:
    """The brake row on the wire: every ``GuardrailsState`` field, FLAT and under its
    OWN column name (so a new column can never be silently dropped by a
    hand-maintained mapping).

    The round-trip claim is scoped to the SIX EDITABLE keys -- the four breakers and
    the two ``hwm_*`` anchors: those come back under exactly the names the POST edit
    body accepts, so the panel's form needs no translation layer. ``state`` /
    ``trip_id`` / ``trip_reason`` / ``sweep_state`` are READ-ONLY here: they move
    through the state machine (halt / trip / clear) and ``edit_limits``' whitelist
    refuses them by name, so posting one back is a 422, not a round trip.

    ``disabled_play_types`` round-trips through its OWN verb (``action:
    'set_scope'``, body key ``disabled``), not through the edit form -- so it is a
    LIST here, sorted, matching the canonical order the column stores. Sorted rather
    than set-shaped because JSON has no set and an arbitrary order would make the
    panel's diffing (and this endpoint's own tests) depend on iteration luck."""
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
        "disabled_play_types": sorted(g.disabled_play_types),
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
    COST: one ``latest_run_date`` select (evaluated eagerly, whatever the snapshot
    says) plus AT MOST one query per breaker that is actually SET -- an all-unset row
    costs exactly the one, since ``breached_breaker`` skips each unset check."""
    hit = guardrails_repo.breached_breaker(
        session, g, run_date=latest_run_date(session) or date.today())
    return None if hit is None else {"breaker": hit[0], "reason": hit[1]}


def _after_write(session: Session, *, action: str) -> dict[str, object]:
    """The response to a brake write that has ALREADY COMMITTED. Always a 200.

    Every ``guardrails_repo`` transition commits before returning, and this endpoint
    then enriches the answer with a fresh snapshot + the live breach evaluation --
    two more DB reads, AFTER the point of no return. Letting those ride the
    app-level 503 handler would tell the operator their action FAILED while the
    brake had in fact moved, and ``clear_trip`` makes that lie dangerous: the
    operator reads 503, retries, gets 409 'trip id is stale or state is not
    tripped', and concludes the brake is still on -- while real money is armed. The
    retry's own error CONFIRMS the wrong story, so nothing self-corrects.

    So the enrichment is best-effort: on failure the answer is still 200, still says
    ``committed: true``, and carries ``enrichment_error`` (same class-only wording
    and ``database error (`` prefix the real handler uses). The state keys are then
    ABSENT rather than guessed -- we could not read them, and the pre-write snapshot
    would actively misdescribe the row we just changed; the client re-polls
    ``GET /api/guardrails``, which is one poll tick away anyway.

    ``committed`` (not ``cleared``): one name for one meaning across all four
    DB-only actions -- edit, set_scope and clear_halt have the same
    commit-then-enrich shape, and an action-specific key would have to be read
    differently per branch."""
    try:
        g = guardrails_repo.peek_guardrails(session)
        breach = _current_breach(session, g)
    except SQLAlchemyError as exc:
        log.warning("guardrail %s committed, but the follow-up read failed -- "
                    "answering 200 with enrichment_error (the write STANDS)",
                    action, exc_info=True)
        _rollback_quietly(session, what=f"the post-{action} read")
        return {"action": action, "committed": True, "current_breach": None,
                "enrichment_error": _db_error_detail(exc)}
    return _state_body(g) | {"action": action, "committed": True,
                             "current_breach": breach, "enrichment_error": None}


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

    # --- the advisory autonomy gate ---------------------------------------------------

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

    # --- DISARM: the raw sweep and the trip-aware resume ------------------------------

    def _resume_disarm(
        session: Session, broker: BrokerClient, g: GuardrailsState, *, dry_run: bool,
    ) -> dict[str, object] | None:
        """DISARM on a TRIPPED book whose sweep never finished: resume THAT sweep.

        Returns None when the resume did NOT happen -- the brake was cleared or
        swept by another process between this request's peek and the resume's own
        state read, or the DB refused that read. The caller then runs the RAW sweep:
        a DISARM is UNCONDITIONAL. Answering "200, nothing happened" because the
        brake moved under us would leave resting entry orders working at the venue
        on the one request whose entire purpose is to pull them. Falling through is
        safe because every raise point inside the resume (its ``load_guardrails``,
        its trip-event fetch) precedes any venue call -- the sweep body itself never
        raises, it records 'partial'.

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

        A sweep that did NOT reach 'complete' is a 503, mirroring the raw disarm's
        partial posture. ``resume_incomplete_sweep`` deliberately swallows venue
        failures (it records 'partial' and returns True -- correct for a background
        cycle, which simply retries next hour), but a HUMAN just pressed DISARM: a
        200 is read as "the book is safe", and here the book demonstrably is not.
        The detail is the sweep's OWN recorded text, which is already class-name-only
        on a failure (``_run_sweep``'s leak posture), and the ``finally`` has still
        invalidated the snapshot, bumped the wake nonce, and left the DisarmEvent the
        sweep wrote -- a partial run moved venue state and must be visible everywhere.

        ``dry_run`` previews: ``resume_incomplete_sweep`` has no dry-run mode (it is
        the response protocol, not a query), so the preview composes the two disarm
        helpers directly with ``dry_run=True`` under the key the real resume WOULD
        use. That is the only path here with itemized COUNTS; a REAL resume cannot
        itemize them (the pipeline returns a bool), so they ride ``detail`` -- the
        sweep event's own recorded summary, verbatim -- and those arrays stay empty.
        ``unprotected`` is the exception: it is an ALARM, not a count, so the real
        resume reports it structurally off the sweep event's ``values_json`` (see
        ``_sweep_record``) rather than leaving it as prose the client would have to
        parse. The full row is in the guardrails event history either way.

        WIRE (the ``mode``-tagged union -- see ``disarm_book``): ``resume_key`` is
        the venue-side client_order_id prefix both processes derive from the trip.
        It is diagnostic detail, given its own field rather than embedded in
        ``detail`` prose so the panel can hide it behind a disclosure instead of
        showing an operator a raw order key mid-emergency.
        """
        trip_id = g.trip_id
        assert trip_id is not None  # the caller gates on this
        key = f"guardrail-{trip_id}"
        if dry_run:
            preview, sells = pull_entry_orders(broker, dry_run=True)
            restored, unprotected = ensure_stop_protection(
                broker, lambda sym: latest_recorded_stop(session, sym),
                key_suffix=key, dry_run=True)
            return {
                "dry_run": True,
                "mode": "guardrail-resume-preview",
                "state": g.state,
                "trip_id": trip_id,
                "sweep_state": g.sweep_state,
                "resume_key": key,
                "detail": f"would resume the sweep for trip {trip_id}",
                "cancelled": [{"symbol": o.symbol,
                               "broker_order_id": o.broker_order_id}
                              for o in preview],
                "sells_kept": len(sells),
                "stops_restored": restored,
                "unprotected": unprotected,
            }
        ran = False
        try:
            # Both DB reads sit inside the guard: the watermark and the resume's own
            # state load precede every venue call, so a refusal here has moved
            # NOTHING and the raw sweep must still run.
            watermark = _event_watermark(session)
            ran = gpipe.resume_incomplete_sweep(
                session, broker=broker, source="cockpit")
        except SQLAlchemyError:
            log.warning("guardrail resume could not read the brake row -- falling "
                        "back to the raw disarm sweep (a DISARM is unconditional)",
                        exc_info=True)
            _rollback_quietly(session, what="the guardrail resume read")
            return None
        finally:
            # The resume swallows venue errors, but it can still have moved venue
            # state before one -- same posture as the raw path: invalidate FIRST
            # (a woken fetch must never hit the stale cache), then wake. Only when a
            # sweep actually ran: a stand-down touched nothing.
            if ran:
                broker_snapshot.invalidate()
                action_nonce.bump()
        if not ran:
            # The trip was cleared (or its sweep finished) between our peek and the
            # resume's own read. Nothing was swept -- fall through to the raw sweep.
            log.info("guardrail resume stood down (state moved mid-request) -- "
                     "running the raw disarm sweep instead")
            return None
        after = guardrails_repo.peek_guardrails(session)
        detail, unprotected = _sweep_record(session, after=watermark)
        if after.sweep_state != "complete":
            # A human pressed DISARM and the book is NOT clean: never a 200.
            raise HTTPException(
                status_code=503,
                detail=f"{_D503_SWEEP} (sweep_state={after.sweep_state}): {detail}")
        return {
            "dry_run": False,
            "mode": "guardrail-resume",
            "state": after.state,
            "trip_id": trip_id,
            "sweep_state": after.sweep_state,
            "resume_key": key,
            "detail": detail,
            # The counts are not itemized on this path (the pipeline returns a
            # bool, not the orders): ``detail`` carries the sweep's own recorded
            # summary and the guardrails event history has the row. Kept as empty
            # lists rather than dropped so the wire stays a superset of a plain
            # disarm. ``unprotected`` is the ONE exception and it is deliberate:
            # a position left with no stop anywhere is an ALARM, and an alarm that
            # only exists as prose inside ``detail`` cannot be branched on -- the
            # panel would render the identical venue state loudly on the raw path
            # and calmly here. It comes off the sweep event's values_json.
            "cancelled": [],
            "sells_kept": 0,
            "stops_restored": [],
            "unprotected": unprotected,
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

        THE ROUTING NEVER BLOCKS THE SWEEP. DISARM is the emergency path and it
        predates the brake: before Task 15 it reached the venue without reading the
        database at all, and it must keep doing so. A refused/failed brake read is
        logged and IGNORED -- the raw sweep below runs -- and a resume that stands
        down (the trip cleared mid-request) falls through to that same sweep rather
        than answering "200, nothing happened". The only DB failure that may stop
        this endpoint is one raised by the sweep itself, AFTER the entry orders are
        already cancelled.

        WIRE: a ``mode``-TAGGED UNION, and ``mode`` alone is what a client branches
        on -- ``raw`` (the body below: itemized ``cancelled`` / ``sells_kept`` /
        ``stops_restored`` / ``unprotected``), ``guardrail-resume`` (the counts ride
        ``detail``; those arrays are empty because the pipeline returns a bool, NOT
        because nothing moved) or ``guardrail-resume-preview``. All three carry
        ``dry_run`` + ``state`` (the brake state this ran against; null when the
        brake row could not be read) -- and all three report ``unprotected``
        FAITHFULLY, so one client-side alarm rule covers every mode.

        503 DETAILS: three stable prefixes, and the only thing a client may branch
        on -- ``database error (``, ``broker error (``, ``guardrail sweep did not
        complete``. See the module constants.
        """
        if not disarm_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail=_ACTION_IN_FLIGHT)
        try:
            try:
                broker = resolved_broker_factory()
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail=broker_error_detail(exc)
                ) from exc
            if broker is None:
                raise HTTPException(status_code=409, detail=_NO_BROKER)
            try:
                g = guardrails_repo.peek_guardrails(session)
            except SQLAlchemyError:
                # A dead/refused DB must not cost the operator their emergency
                # sweep: pre-Task-15 this endpoint read no DB before the venue, and
                # that guarantee is restored here rather than defended downstream.
                log.warning("could not read the brake row -- running the raw disarm "
                            "sweep (a DISARM is unconditional)", exc_info=True)
                g = None
                _rollback_quietly(session, what="the brake read")
            # ``gpipe._INCOMPLETE_SWEEPS`` rather than a literal: ONE definition of
            # "the sweep has not finished" across the pipeline and the cockpit.
            # ``trip_id is None`` on a tripped row is never expected -- if it ever
            # happens the resume could not key a sweep outcome anyway, so fall
            # through to the raw sweep (protection now beats bookkeeping).
            if (g is not None and g.state == "tripped"
                    and g.sweep_state in gpipe._INCOMPLETE_SWEEPS
                    and g.trip_id is not None):
                resumed = _resume_disarm(session, broker, g, dry_run=dry_run)
                if resumed is not None:
                    return resumed
                # None = the resume did not happen; the raw sweep below still must.
            # Pre-bound so the FAILURE path can report how many entries were pulled
            # before the raise: pull_entry_orders raising mid-cancel leaves the name
            # unbound, and 0 ("we don't know that any cancel landed") is the honest
            # floor -- never an invented count. ``unprotected`` is pre-bound for the
            # same reason: the reason label below reads it, and a raise before the
            # ensure pass leaves it unset (that path is 'cockpit-partial' anyway).
            entries: list[BrokerOrder] = []
            unprotected: list[str] = []
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
                    # entries then left the book part-protected has moved MORE
                    # alarming venue state than a clean one, not less. So
                    # 'cockpit' is earned, not assumed: the sweep must have
                    # COMPLETED *and* left nothing unprotected. A raise (the sweep
                    # died) and a completed-but-partial pass (since
                    # ensure_stop_protection's per-position boundary, a refused
                    # stop re-submit reports the symbol instead of raising) both
                    # read 'cockpit-partial' -- the Auditor's disarm narrative
                    # treats that as the more-alarming context deliberately, and
                    # partially-protected venue state is exactly that. Best-effort
                    # (_record_disarm never raises), so a failed event write can
                    # never mask the 503.
                    _record_disarm(
                        session,
                        reason=("cockpit" if completed and not unprotected
                                else "cockpit-partial"),
                        orders_cancelled=len(entries))
            return {
                "dry_run": dry_run,
                "mode": "raw",
                # The brake state this swept against -- null when the row could not
                # be read (the emergency path ran anyway). Never a guess.
                "state": g.state if g is not None else None,
                "cancelled": [{"symbol": o.symbol,
                               "broker_order_id": o.broker_order_id}
                              for o in entries],
                "sells_kept": len(sells),
                "stops_restored": restored,
                "unprotected": unprotected,
            }
        finally:
            disarm_lock.release()

    # --- the Execution Safety report --------------------------------------------------

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

    # --- the guardrails brake: state, history, and the four operator actions ----------

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
        """The brake's five operator actions. Header-guarded like every mutation.

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

        Its mirror image is the SECOND hard rule: once a transition has committed,
        the answer is ALWAYS a 200 (see ``_after_write``) -- a DB failure in the
        follow-up read may not turn a durable change into a reported failure.

        * ``edit`` -- the six limit columns (``guardrails_repo``'s whitelist is the
          gate; unknown key / non-positive breaker -> 422 carrying the repo's own
          reason). Presence decides: an omitted key is untouched, an explicit null
          unsets. State columns can never ride an edit, so editing a cap while
          tripped leaves the brake tripped.
        * ``halt`` -- 'ok' -> 'halted' AND the protective sweep, so a REAL run takes
          the SAME single-flight ``disarm_lock`` /api/disarm holds (409 to the
          loser, before the factory resolves). The DB brake lands FIRST --
          persist-first, exactly like ``respond_to_trip``: the halt blocks every
          dispatch path on DB truth alone, and the venue sweep is best-effort on top
          (no broker -> still 200, ``sweep.ran`` False). ``dry_run=1`` previews and
          changes NOTHING -- not the state, not a row, not the venue -- and
          deliberately does NOT hold the lock (see ``_halt_action``).
        * ``clear_halt`` -- 'halted' -> 'ok', DB-only. A trip never clears here.
          ``dry_run`` is a 422 on this and the other three DB-only actions: there
          is nothing to preview, and silently ignoring the flag would turn a preview
          into an execution.
        * ``clear_trip`` -- requires ``ack_trip_id``: the clear only matches the trip
          the operator actually READ, so a stale cockpit screen cannot release a
          newer trip. The response carries ``current_breach`` so the panel can say
          immediately that this will re-trip within the hour.
        * ``set_scope`` -- the Strategy Board's tighten-only subtraction, DB-only:
          body ``disabled`` is the WHOLE new set of play types the cockpit switches
          OFF (``[]`` re-enables everything the env ceiling still allows; an unknown
          play type is a 422 carrying the repo's own reason). It can only ever
          SUBTRACT from ``SWING_EXECUTE_PLAY_TYPES`` -- widening the ceiling stays an
          env/IaC act -- so there is nothing here that can arm what was not already
          armed. It has NO 409: unlike halt/clear the write is an unconditional
          idempotent SET, so pressing "disable" on an already-disabled strategy
          succeeds (and, exactly like a no-op ``edit``, still journals its event --
          "the operator asked" is itself the audit fact).

        AS-BUILT DEVIATION (the plan said ``halt``/``clear_trip`` both take the
        lock): ``clear_trip`` does NOT. The lock exists to serialise protective stop
        SUBMITS at the venue -- two overlapping runs can each read the open-order
        list before either acts, and duplicate live GTC sell stops on a margin
        account close a position and then SHORT it. ``clear_trip`` touches no venue
        at all; its own race is handled far better by the repo's conditional UPDATE
        (``WHERE trip_id = <acknowledged> AND state = 'tripped'``), which is atomic
        across PROCESSES, where a per-process lock is not. Holding the lock here
        would only let a clear 409 a concurrent emergency DISARM.

        503 DETAILS: three stable prefixes, and the only thing a client may branch
        on -- ``database error (``, ``broker error (``, ``guardrail sweep did not
        complete``. See the module constants.
        """
        if dry_run and body.action != "halt":
            # HALT is the only action with a venue side to preview; the other four
            # are pure DB transitions. Refusing loudly rather than ignoring the flag
            # matters because the frontend fires the preview on every hold-START: a
            # silently-ignored dry_run would EXECUTE the action -- a "preview" that
            # released the brake.
            raise HTTPException(status_code=422,
                                detail="dry_run is only supported for halt")

        if body.action == "edit":
            fields = {k: getattr(body, k)
                      for k in sorted(body.model_fields_set - _NON_LIMIT_KEYS)}
            try:
                guardrails_repo.edit_limits(session, source="cockpit", **fields)
            except (ValueError, TypeError) as exc:
                # ValueError = the repo whitelist / positivity rules. TypeError =
                # a body key that COLLIDES with edit_limits' own parameters
                # ('source', 'session'): Python raises before the whitelist ever
                # runs, and an uncaught one would 500 the brake's write endpoint.
                # Both are client errors carrying the repo's own reason.
                raise HTTPException(
                    status_code=422, detail=_client_error(exc)) from exc
            action_nonce.bump()
            return _after_write(session, action="edit")

        if body.action == "halt":
            return _halt_action(session, dry_run=dry_run)

        if body.action == "set_scope":
            if body.disabled is None:
                # Presence, not truthiness: `[]` is the legitimate re-enable-all
                # request and must never be confused with an omitted key. A
                # set_scope that silently defaulted to "" would RESTORE every
                # disabled strategy on a malformed body -- the one direction this
                # feature may never move without being asked.
                raise HTTPException(
                    status_code=422,
                    detail="set_scope requires disabled (a list of play types; "
                           "[] re-enables everything the env ceiling allows)")
            try:
                guardrails_repo.set_disabled_play_types(
                    session, disabled=set(body.disabled), source="cockpit")
            except ValueError as exc:
                # The repo's vocabulary validation is THE gate (one definition,
                # shared with any future caller); its message names the invalid
                # member and the valid set, so it is the operator's message too.
                raise HTTPException(
                    status_code=422, detail=_client_error(exc)) from exc
            action_nonce.bump()
            return _after_write(session, action="set_scope")

        if body.action == "clear_halt":
            if not guardrails_repo.clear_halt(session, source="cockpit"):
                state = guardrails_repo.peek_guardrails(session).state
                raise HTTPException(
                    status_code=409,
                    detail=f"nothing to clear — no HALT is in force "
                           f"(state: {state})")
            action_nonce.bump()
            return _after_write(session, action="clear_halt")

        if body.ack_trip_id is None:
            raise HTTPException(status_code=422,
                                detail="clear_trip requires ack_trip_id")
        if not guardrails_repo.clear(session, acknowledged_trip_id=body.ack_trip_id,
                                     source="cockpit"):
            raise HTTPException(
                status_code=409, detail="trip id is stale or state is not tripped")
        action_nonce.bump()
        # The release has COMMITTED: from here the answer is a 200 whatever the DB
        # does. See _after_write -- on this action the alternative is telling an
        # operator the brake is still on while real money is armed.
        return _after_write(session, action="clear_trip")

    def _halt_action(session: Session, *, dry_run: bool) -> dict[str, object]:
        """The HALT branch: single-flight, persist-first, sweep best-effort.

        THE LOCK COVERS REAL RUNS ONLY. It exists to serialise protective stop
        SUBMITS -- two overlapping runs can each read the open-order list before
        either acts, and the per-second key suffix only collapses re-submits inside
        the same second, so duplicate live GTC sell stops are the hazard. A dry run
        submits NOTHING: it makes two read-only venue round-trips and returns. The
        frontend fires it on every hold-START, and holding the lock across those
        round-trips would let a preview -- possibly an abandoned one -- 409 the
        emergency DISARM the operator reaches for next. Previews must never be able
        to block the brake. Real runs take it non-blocking and release it in the
        outer ``finally``, so a failed halt never wedges the endpoint shut.

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
        if dry_run:
            # LOCK-FREE by design (see above): a preview submits nothing, so it must
            # never be able to 409 the emergency DISARM. Peek (no seed), no
            # transition, and the venue read through the two disarm helpers'
            # dry-run mode -- Task 16 fires this on every hold-START, so it stays
            # free of side effects.
            try:
                broker = resolved_broker_factory()
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail=broker_error_detail(exc)) from exc
            g = guardrails_repo.peek_guardrails(session)
            if broker is None:
                sweep = _sweep_result(ran=False, detail=_NO_BROKER)
            else:
                preview, sells = pull_entry_orders(broker, dry_run=True)
                restored, unprotected = ensure_stop_protection(
                    broker, lambda sym: latest_recorded_stop(session, sym),
                    key_suffix=_halt_key(), dry_run=True)
                sweep = _sweep_result(ran=False, entries=preview, sells=len(sells),
                                      restored=restored, unprotected=unprotected)
            # ``committed: False`` keeps the POST responses one shape: every one of
            # them says whether the brake actually moved, and only this branch says
            # it did not.
            return _state_body(g) | {
                "action": "halt", "committed": False, "dry_run": True,
                "current_breach": _current_breach(session, g),
                "enrichment_error": None, "sweep": sweep}

        if not disarm_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail=_ACTION_IN_FLIGHT)
        # Pre-bound so the FAILURE path can report how many entries were pulled
        # before the raise: ``pull_entry_orders`` dying mid-cancel leaves the name
        # unbound, and 0 ("we don't know that any cancel landed") is the honest
        # floor -- never an invented count. Same reasoning as /api/disarm's.
        entries: list[BrokerOrder] = []
        try:
            halted = False
            venue_touched = False
            try:
                # The PRIMARY state write, and it goes FIRST: a SQLAlchemyError here
                # rides the app-level 503 handler -- deliberately NOT caught.
                if not guardrails_repo.halt(session, source="cockpit"):
                    state = guardrails_repo.peek_guardrails(session).state
                    raise HTTPException(
                        status_code=409,
                        detail=f"nothing to halt — the brake is already engaged "
                               f"(state: {state})")
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
            # The halt has COMMITTED (and the sweep has run), so the answer is a 200
            # whatever the follow-up reads do -- see _after_write. ``sweep`` rides
            # ON TOP, so even an enrichment failure still reports what the venue did.
            return _after_write(session, action="halt") | {
                "dry_run": False, "sweep": sweep}
        finally:
            disarm_lock.release()

    # --- the live configuration (read-only) -------------------------------------------

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

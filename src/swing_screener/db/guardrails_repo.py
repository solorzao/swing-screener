"""Guardrails state machine + breaker queries for the live agent's brake.

Every state change is an ATOMIC conditional UPDATE (rows-affected election)
plus one appended ``AgentGuardrailEvent``; every read on the hot dispatch path
is a COLUMN select (never a cached ORM entity, which the dispatch loop's
long-lived Session would serve stale).

Reads come in two flavours and the difference is load-bearing:
``load_guardrails`` GET-OR-CREATES (enforcement + anything about to UPDATE --
the row must exist or the conditional UPDATE matches nothing), while
``peek_guardrails`` never writes (read-only surfaces: preflight, the cockpit
polls). Same snapshot type, same freshness; only the empty-table behaviour differs. The election on ``trip`` is the ONLY
cross-process lock the brake has -- the 8am digest job, the 4:15pm screen job
and the cockpit all race the same WHERE clause, and whoever's UPDATE reports
rowcount 1 owns the trip response (sweep + email); everyone else stands down.

SQL Server portability (repo law): string comparisons render via ``==`` /
``.in_()`` (``col = 'x'``), never a boolean ``.is_()``; ``.is_not(None)`` for
NULL checks is fine.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, ScalarSelect, Update, func, select, update
from sqlalchemy.orm import Session

from swing_screener.db.models import AgentGuardrailEvent, AgentGuardrails, ExitEvent, PaperTrade
from swing_screener.db.repo import execution_logs_for_day
from swing_screener.settings import Settings

log = logging.getLogger(__name__)

# the one account whose trades are real money -- every breaker below is pinned to it.
LIVE_ACCOUNT = "live"

# the ONLY columns edit_limits may set. state / trip_id / trip_reason / sweep_state
# move through the state machine (trip / clear / halt), NEVER through an edit --
# the whitelist is what makes that a guarantee rather than a convention.
_EDITABLE_LIMITS = (
    "max_daily_loss_usd",
    "max_trades_per_day",
    "max_drawdown_usd",
    "loss_streak_halt",
    "hwm_anchor_date",
    "hwm_baseline_usd",
)
# the breakers among them that must be POSITIVE numbers when set (None = unset
# stays legal): a zero or negative cap would trip on the first close, and a
# negative streak would trip on an empty book. The two hwm_* anchors are
# unconstrained -- a baseline of 0.0 is a legitimate fresh-start anchor.
_POSITIVE_LIMITS = (
    "max_daily_loss_usd",
    "max_trades_per_day",
    "max_drawdown_usd",
    "loss_streak_halt",
)
# the sweep_state vocabulary (matches the model comment); validated BEFORE the
# UPDATE so a typo'd outcome fails loudly on every backend, not just Azure SQL.
_SWEEP_OUTCOMES = ("pending", "partial", "complete")

# the snapshot columns, selected raw so the read NEVER routes through the
# Session's identity map (a long-lived dispatch Session would serve the entity
# it cached before a cockpit HALT landed).
_STATE_COLUMNS = (
    AgentGuardrails.state,
    AgentGuardrails.max_daily_loss_usd,
    AgentGuardrails.max_trades_per_day,
    AgentGuardrails.max_drawdown_usd,
    AgentGuardrails.loss_streak_halt,
    AgentGuardrails.hwm_anchor_date,
    AgentGuardrails.hwm_baseline_usd,
    AgentGuardrails.trip_id,
    AgentGuardrails.trip_reason,
    AgentGuardrails.sweep_state,
)


@dataclass(frozen=True)
class GuardrailsState:
    """A point-in-time COLUMN snapshot of the single agent_guardrails row.

    Deliberately not the ORM entity: a snapshot can't go stale in an identity
    map, and it can't be mutated and flushed by accident. Fields mirror the
    table minus ``id`` / ``updated_at``.
    """

    state: str
    max_daily_loss_usd: float | None
    max_trades_per_day: int | None
    max_drawdown_usd: float | None
    loss_streak_halt: int | None
    hwm_anchor_date: date | None
    hwm_baseline_usd: float
    trip_id: int | None
    trip_reason: str | None
    sweep_state: str | None


def _canonical_row_id() -> "ScalarSelect[Any]":
    """The canonical row's id, as a scalar subquery: MIN(id), oldest row wins.

    Every conditional UPDATE pins on this so rowcount is capped at 1 even if the
    empty-table seed race ever leaves TWO rows -- and MIN (not MAX) so writes
    land on the SAME row the ascending read in ``load_guardrails`` returns.
    ``.correlate(None)`` keeps the subquery self-contained inside an UPDATE on
    the same table: auto-correlation would drop its FROM and turn the predicate
    into a per-row ``id = min(id)`` tautology.
    """
    return select(func.min(AgentGuardrails.id)).correlate(None).scalar_subquery()


def _select_state(session: Session) -> GuardrailsState | None:
    row = session.execute(
        select(*_STATE_COLUMNS).order_by(AgentGuardrails.id).limit(1)
    ).first()
    if row is None:
        return None
    return GuardrailsState(**row._mapping)


#: The state a brake row that does not exist yet WOULD have: byte-identical to the
#: row ``load_guardrails`` seeds (the ``AgentGuardrails`` column defaults -- state
#: 'ok', every breaker unset, baseline 0.0, no trip). It exists so a READ-ONLY caller
#: (``peek_guardrails``) can answer honestly without writing: on an empty table the
#: only truthful answer IS the default, and inventing it here beats an INSERT the
#: caller never asked for. Keep in lockstep with the model's defaults.
_UNSEEDED = GuardrailsState(
    state="ok",
    max_daily_loss_usd=None,
    max_trades_per_day=None,
    max_drawdown_usd=None,
    loss_streak_halt=None,
    hwm_anchor_date=None,
    hwm_baseline_usd=0.0,
    trip_id=None,
    trip_reason=None,
    sweep_state=None,
)


def peek_guardrails(session: Session) -> GuardrailsState:
    """The current brake state, READ-ONLY: the column select, or ``_UNSEEDED``.

    Same snapshot ``load_guardrails`` returns, minus the get-or-create: an empty
    table reads as the default row instead of creating one. For SURFACES -- preflight,
    the cockpit polls -- which must not write: a poll that INSERTs would 503 the
    masthead under a read-only DB grant, and a read-only check has no business
    materialising rows.

    NEVER call this before an UPDATE. Every transition (``trip`` / ``halt`` /
    ``edit_limits``) get-or-creates FIRST precisely so its conditional UPDATE has a
    target row; peeking there would leave the UPDATE matching nothing, and the brake
    would silently no-op -- a halt that reports success and blocks nothing. Enforcement
    paths keep ``load_guardrails``.
    """
    state = _select_state(session)
    return state if state is not None else _UNSEEDED


def load_guardrails(session: Session) -> GuardrailsState:
    """The current brake state, get-or-creating the single default row.

    A raw column select of the OLDEST row (``order_by(id).limit(1)`` -- the
    canonical row; there is normally only one), bypassing the identity map so a
    change committed by ANOTHER process/session (a cockpit HALT mid-dispatch)
    is always visible. Every write is pinned to the same MIN(id) row (see
    ``_canonical_row_id``), so reads and writes agree even if the empty-table
    seed race ever leaves a stray second row. If no row exists yet, seed the
    default row WITHOUT an explicit id: on SQL Server the PK is IDENTITY, and
    an explicit id needs IDENTITY_INSERT permission the prod managed identity
    may lack (see the model docstring).
    """
    state = _select_state(session)
    if state is not None:
        return state
    session.add(AgentGuardrails(updated_at=datetime.now(UTC)))
    session.commit()
    seeded = _select_state(session)
    assert seeded is not None  # we just committed the row
    return seeded


def _event(
    *, kind: str, source: str, breaker: str = "", reason: str = "",
    values_json: str = "{}",
) -> AgentGuardrailEvent:
    """Build one event row, truncated to the column bounds.

    sqlite never enforces ``String(N)`` but Azure SQL raises -- and in ``trip``
    the event insert runs BEFORE the state UPDATE, so an overlong breaker or
    source would keep the brake from engaging in PROD only. Truncation makes
    the append infallible on both backends.
    """
    return AgentGuardrailEvent(
        created_at=datetime.now(UTC), kind=kind, breaker=breaker[:32],
        reason=reason[:256], values_json=values_json, source=source[:16],
    )


def record_event(
    session: Session, *, kind: str, source: str, breaker: str = "",
    reason: str = "", values_json: str = "{}",
) -> int:
    """Append one AgentGuardrailEvent row and commit; returns its id."""
    event = _event(kind=kind, source=source, breaker=breaker, reason=reason,
                   values_json=values_json)
    session.add(event)
    session.commit()
    session.refresh(event)
    return event.id


def _plain_update(session: Session, stmt: Update) -> int:
    """Execute one conditional UPDATE in the CURRENT transaction; returns rowcount.

    ``synchronize_session=False``: with a scalar subquery in the WHERE (the
    MIN-id pin), the ORM's 'auto' sync falls back to 'fetch', which injects
    RETURNING/OUTPUT and makes rowcount ride three layers of SQLAlchemy
    internals (and OUTPUT hard-errors on SQL Server if the table ever gains a
    trigger). No read in this module uses ORM entities, so there is nothing to
    synchronize -- plain UPDATE, DBAPI-native rowcount on both backends.

    Does NOT commit: the caller owns the transaction, so a state change and
    its audit event can share one commit (a crash can never leave a transition
    with no audit row). `Session.execute` is typed `Result`; an UPDATE actually
    yields a `CursorResult`, which is what carries `rowcount` (repo.py's cast).
    """
    result = session.execute(stmt.execution_options(synchronize_session=False))
    return cast("CursorResult[Any]", result).rowcount


def trip(session: Session, *, breaker: str, reason: str, source: str) -> int | None:
    """Trip the brake; the rows-affected election picks exactly ONE owner.

    The 'trip' event is appended FIRST, unconditionally -- it is true that the
    breaker breached, whoever wins the race. Then the conditional UPDATE
    (``WHERE state != 'tripped'``): rowcount 1 means this caller owns the trip
    response (sweep + email) and gets the event id back; rowcount 0 means
    someone else already tripped and owns it -- return None, stand down. A
    'halted' state IS overwritten: the trip is the stronger record (a breach
    happened; the halt's dispatch block is preserved either way).

    DELIBERATELY two transactions (the event commits before the UPDATE), unlike
    every other transition: the event id must exist to become ``trip_id``, and
    the breach record must survive even if this process dies mid-election.
    """
    load_guardrails(session)  # get-or-create so the UPDATE has a target
    eid = record_event(session, kind="trip", source=source, breaker=breaker,
                       reason=reason)
    rowcount = _plain_update(
        session,
        update(AgentGuardrails)
        .where(
            AgentGuardrails.id == _canonical_row_id(),
            AgentGuardrails.state != "tripped",
        )
        .values(state="tripped", trip_id=eid, trip_reason=reason[:256],
                sweep_state="pending", updated_at=datetime.now(UTC)),
    )
    session.commit()
    return eid if rowcount == 1 else None


def clear(session: Session, *, acknowledged_trip_id: int, source: str) -> bool:
    """Release a trip -- but ONLY the trip the operator actually acknowledged.

    ``WHERE trip_id == acknowledged_trip_id AND state == 'tripped'``: a stale
    ack (the brake re-tripped since the operator looked) matches nothing and
    the brake stays on. No event on a failed clear -- nothing changed. Halts
    release via ``clear_halt``, never through here. State change + audit event
    share ONE commit, so a crash can't release the brake without its record.
    """
    rowcount = _plain_update(
        session,
        update(AgentGuardrails)
        .where(
            AgentGuardrails.id == _canonical_row_id(),
            AgentGuardrails.trip_id == acknowledged_trip_id,
            AgentGuardrails.state == "tripped",
        )
        .values(state="ok", trip_id=None, trip_reason=None, sweep_state=None,
                updated_at=datetime.now(UTC)),
    )
    if rowcount != 1:
        session.rollback()  # end the no-op write txn
        return False
    session.add(_event(kind="clear", source=source,
                       reason=f"trip {acknowledged_trip_id} acknowledged and cleared"))
    session.commit()
    return True


def halt(session: Session, *, source: str, reason: str = "manual HALT") -> bool:
    """Manual brake: 'ok' -> 'halted'. Never downgrades a trip.

    ``WHERE state == 'ok'``: a tripped brake stays tripped (the trip record --
    trip_id / sweep bookkeeping -- must survive until its own clear). Event on
    success only; returns False when the state was not 'ok'. State change +
    audit event share ONE commit.
    """
    load_guardrails(session)  # get-or-create so the UPDATE has a target
    rowcount = _plain_update(
        session,
        update(AgentGuardrails)
        .where(
            AgentGuardrails.id == _canonical_row_id(),
            AgentGuardrails.state == "ok",
        )
        .values(state="halted", updated_at=datetime.now(UTC)),
    )
    if rowcount != 1:
        session.rollback()  # end the no-op write txn
        return False
    session.add(_event(kind="halt", source=source, reason=reason))
    session.commit()
    return True


def clear_halt(session: Session, *, source: str) -> bool:
    """Release a manual halt: 'halted' -> 'ok'. Trips don't clear through here.

    State change + audit event share ONE commit.
    """
    rowcount = _plain_update(
        session,
        update(AgentGuardrails)
        .where(
            AgentGuardrails.id == _canonical_row_id(),
            AgentGuardrails.state == "halted",
        )
        .values(state="ok", updated_at=datetime.now(UTC)),
    )
    if rowcount != 1:
        session.rollback()  # end the no-op write txn
        return False
    session.add(_event(kind="clear", source=source, reason="HALT cleared"))
    session.commit()
    return True


def edit_limits(session: Session, *, source: str, **limits: object) -> None:
    """Set breaker limits / the drawdown anchor -- and NOTHING else.

    Only the whitelisted limit columns pass (ValueError otherwise), so the
    state columns can never ride through an edit: editing a cap while tripped
    leaves the brake tripped. The four breakers must be POSITIVE when set
    (None = unset stays legal). Sets exactly the passed keys + ``updated_at``
    and appends one 'edit' event whose values_json carries ``{"old": ...,
    "new": ...}`` for those keys (dates as isoformat) -- UPDATE and event
    share ONE commit.
    """
    unknown = [k for k in limits if k not in _EDITABLE_LIMITS]
    if unknown:
        raise ValueError(
            f"edit_limits: not an editable limit column: {', '.join(sorted(unknown))}"
        )
    if not limits:
        raise ValueError("edit_limits: no limits passed")
    for key in _POSITIVE_LIMITS:
        if key in limits and limits[key] is not None:
            value = limits[key]
            if not isinstance(value, int | float) or value <= 0:
                raise ValueError(
                    f"edit_limits: {key} must be a positive number or None "
                    f"(unset), got {value!r}"
                )

    old = load_guardrails(session)  # get-or-create + the old values for the event

    def _jsonable(value: object) -> object:
        return value.isoformat() if isinstance(value, date) else value

    old_values = {k: _jsonable(getattr(old, k)) for k in limits}
    new_values = {k: _jsonable(v) for k, v in limits.items()}
    _plain_update(
        session,
        update(AgentGuardrails)
        .where(AgentGuardrails.id == _canonical_row_id())
        .values(updated_at=datetime.now(UTC), **limits),
    )
    session.add(_event(
        kind="edit", source=source,
        reason="limits edited: " + ", ".join(sorted(limits)),
        values_json=json.dumps({"old": old_values, "new": new_values},
                               sort_keys=True),
    ))
    session.commit()


def record_sweep_outcome(
    session: Session, *, trip_id: int, outcome: str, detail: str, source: str
) -> None:
    """Record how the trip's sweep ended: 'complete' | 'partial' | 'pending'.

    Keys on ``trip_id`` (not blindly on the row) so a sweep finishing late
    never stamps a NEWER trip's bookkeeping; appends one 'sweep' event carrying
    the outcome + detail either way (the sweep DID run -- the Auditor sees it
    even if the trip was already cleared). The outcome vocabulary is validated
    up front so a typo fails loudly on every backend, not just Azure SQL
    (sweep_state is String(16)); UPDATE and event share ONE commit.
    """
    if outcome not in _SWEEP_OUTCOMES:
        raise ValueError(
            f"record_sweep_outcome: outcome must be one of {_SWEEP_OUTCOMES}, "
            f"got {outcome!r}"
        )
    _plain_update(
        session,
        update(AgentGuardrails)
        .where(
            AgentGuardrails.id == _canonical_row_id(),
            AgentGuardrails.trip_id == trip_id,
        )
        .values(sweep_state=outcome, updated_at=datetime.now(UTC)),
    )
    session.add(_event(
        kind="sweep", source=source, reason=detail,
        values_json=json.dumps({"trip_id": trip_id, "outcome": outcome}),
    ))
    session.commit()


# ---------------------------------------------------------------- breaker inputs


def trades_today(session: Session, *, run_date: date) -> int:
    """Count of live orders that COUNT against the trades/day breaker today.

    Reuses ``execution_logs_for_day`` so the breaker counts EXACTLY what the
    limit engine counts (submitted_live / filled_live; a skipped clamp or a
    rejected/canceled order never reserved anything).
    """
    return len(execution_logs_for_day(session, run_date=run_date, account=LIVE_ACCOUNT))


def realized_usd_on(session: Session, *, run_date: date) -> float:
    """The day's realized live $: sum of (exit - entry) * qty over closed trades.

    The daily-loss breaker's input. Only closed ``live`` trades whose
    ``exit_date == run_date`` AND that carry a broker-stamped ``qty`` count --
    a NULL ``qty`` (legacy live row) contributes 0, because $ math skips
    unsized rows, never guesses (PaperTrade.qty's contract).
    ``func.coalesce(..., 0.0)`` makes an empty day 0.0 rather than NULL, and
    ``== "closed"`` renders ``col = 'x'`` (portable to SQL Server), not a
    boolean ``.is_()``.
    """
    stmt = select(
        func.coalesce(
            func.sum((PaperTrade.exit_price - PaperTrade.entry_price) * PaperTrade.qty),
            0.0,
        )
    ).where(
        PaperTrade.status == "closed",
        PaperTrade.account == LIVE_ACCOUNT,
        PaperTrade.exit_date == run_date,
        PaperTrade.qty.is_not(None),
        PaperTrade.exit_price.is_not(None),
        PaperTrade.entry_price.is_not(None),
    )
    return float(session.scalar(stmt) or 0.0)


def live_drawdown_usd(
    session: Session, *, anchor_date: date | None, baseline_usd: float
) -> float:
    """Current $ drawdown from the high-water mark of cumulative realized live P&L.

    Closed live trades in ExitEvent order (``ExitEvent.id`` ASC -- the close
    sequence, not the calendar), optionally windowed to ``ExitEvent.created_date
    >= anchor_date``. Unsized rows (NULL qty / prices) are excluded outright --
    a guessed $ figure has no place in a breaker. ``cum`` and the high-water
    mark both start at ``baseline_usd``; the result is ``hwm - cum`` at the end
    of the walk (the CURRENT drawdown, not the max), floored at 0.0.
    """
    stmt = (
        select(PaperTrade.entry_price, PaperTrade.exit_price, PaperTrade.qty)
        .select_from(ExitEvent)
        .join(PaperTrade, ExitEvent.trade_id == PaperTrade.id)
        .where(
            ExitEvent.account == LIVE_ACCOUNT,
            PaperTrade.status == "closed",
            PaperTrade.qty.is_not(None),
            PaperTrade.exit_price.is_not(None),
            PaperTrade.entry_price.is_not(None),
        )
        .order_by(ExitEvent.id)
    )
    if anchor_date is not None:
        stmt = stmt.where(ExitEvent.created_date >= anchor_date)
    cum = hwm = baseline_usd
    for entry_price, exit_price, qty in session.execute(stmt):
        cum += (exit_price - entry_price) * qty
        hwm = max(hwm, cum)
    return max(0.0, hwm - cum)


def live_loss_streak(session: Session) -> int:
    """Consecutive realized_r < 0 live closes, newest first (ExitEvent.id DESC).

    Rows with a NULL ``trade_id`` (orphan events) or a NULL ``realized_r``
    (ungraded close) are SKIPPED -- the scan continues past them rather than
    resetting, so an orphan can neither extend nor break a streak. The first
    ``realized_r >= 0`` stops the scan.
    """
    stmt = (
        select(ExitEvent.trade_id, PaperTrade.realized_r)
        .select_from(ExitEvent)
        .outerjoin(PaperTrade, ExitEvent.trade_id == PaperTrade.id)
        .where(ExitEvent.account == LIVE_ACCOUNT)
        .order_by(ExitEvent.id.desc())
    )
    streak = 0
    for trade_id, realized_r in session.execute(stmt):
        if trade_id is None or realized_r is None:
            continue
        if realized_r < 0:
            streak += 1
        else:
            break
    return streak


def breached_breaker(
    session: Session, g: GuardrailsState, *, run_date: date
) -> tuple[str, str] | None:
    """``(breaker_column_name, reason)`` for the FIRST breached breaker, else None.

    The four per-breaker checks, extracted from ``execution._guardrail_block``
    so the submit-side clamp and the dispatch-loop trip evaluation
    (``pipeline.guardrails.evaluate_breakers``) share ONE definition of
    "breached" -- same order, same thresholds, byte-identical reason strings
    (tests/pipeline/test_execution_guardrails.py pins them, and the reasons echo
    verbatim into cockpit-visible detail/trip_reason). Lives HERE rather than in
    execution.py because it consumes only this module's queries + snapshot type,
    and guardrails_repo must never import the pipeline layer (no cycle can ever
    form). A pure READ + decide: no state consult (state is a trip's OUTCOME,
    not an input), no clamp, no write. Each breaker is skipped when unset."""
    if g.max_trades_per_day is not None:
        n = trades_today(session, run_date=run_date)
        if n >= g.max_trades_per_day:
            return "max_trades_per_day", f"max trades/day: {n} >= {g.max_trades_per_day}"
    if g.max_daily_loss_usd is not None:
        day_usd = realized_usd_on(session, run_date=run_date)
        if day_usd <= -g.max_daily_loss_usd:
            return ("max_daily_loss_usd",
                    f"max daily loss: ${day_usd:.2f} <= -${g.max_daily_loss_usd:.2f}")
    if g.max_drawdown_usd is not None:
        dd = live_drawdown_usd(
            session, anchor_date=g.hwm_anchor_date, baseline_usd=g.hwm_baseline_usd)
        if dd >= g.max_drawdown_usd:
            return ("max_drawdown_usd",
                    f"max drawdown: ${dd:.2f} >= ${g.max_drawdown_usd:.2f}")
    if g.loss_streak_halt is not None:
        s = live_loss_streak(session)
        if s >= g.loss_streak_halt:
            return "loss_streak_halt", f"loss streak: {s} >= {g.loss_streak_halt}"
    return None


def effective_execution_scope(
    settings: Settings, *, session: Session | None
) -> frozenset[str] | None:
    """The set of play types execution may dispatch, or None = unscoped (all).

    Today: purely the env ceiling (``SWING_EXECUTE_PLAY_TYPES``, parsed
    fail-closed in ``load_settings``). ``session`` is REQUIRED keyword-only but
    unused for now -- Task 22 subtracts the cockpit-disabled set here (effective
    = ceiling - disabled), and forcing every caller to hand a session TODAY
    means no call site can silently skip that subtraction when it lands. Lives
    HERE rather than in settings.py because settings stays deliberately
    import-light (a module-level PLAY_TYPES/ORM import there would hand every
    settings importer those edges) and because Task 22's ``disabled_play_types``
    is an ``agent_guardrails`` column this module owns (its tighten-only edit
    walks the same event-audited state machine as every other brake write).
    """
    return settings.execute_play_types


def mandate_from_state(g: GuardrailsState) -> tuple[bool, str]:
    """The mandate, evaluated over a snapshot you already hold. PURE -- no DB, no write.

    Returns ``(True, "")`` only if ``max_daily_loss_usd``, ``max_trades_per_day``
    AND ``max_drawdown_usd`` are ALL set and the brake state is 'ok'. Otherwise
    refuses, naming the FIRST failing item so a misconfig reads as one concrete
    cause (mirrors ``settings.real_money_limits_ok``). ``loss_streak_halt`` is
    optional and never part of the mandate.

    The reason strings are PINNED: they are logged verbatim as ``rejected_live``
    execution details and rendered verbatim on the cockpit's safety screen, so a
    surface that wants extra context appends to them at ITS layer, never here.

    Split out of ``guardrails_mandate_ok`` so a read-only surface can pair it with
    ``peek_guardrails`` (one snapshot, no seed) while enforcement keeps the seeding
    entry point below -- ONE definition of "may real money dispatch", two ways in.
    """
    if g.max_daily_loss_usd is None:
        return False, "max_daily_loss_usd is not set"
    if g.max_trades_per_day is None:
        return False, "max_trades_per_day is not set"
    if g.max_drawdown_usd is None:
        return False, "max_drawdown_usd is not set"
    if g.state != "ok":
        return False, f"guardrails state is {g.state}"
    return True, ""


def guardrails_mandate_ok(session: Session) -> tuple[bool, str]:
    """The ENFORCEMENT entry to the mandate: ``mandate_from_state(load_guardrails(...))``.

    Unchanged behaviour for every enforcement caller (execution's real-money guard,
    the dispatch-loop consult): it get-or-creates the row, so the brake always has a
    target for the UPDATE a trip would issue moments later. Read-only SURFACES use
    ``mandate_from_state(peek_guardrails(session))`` instead -- same verdict, no write.
    """
    return mandate_from_state(load_guardrails(session))

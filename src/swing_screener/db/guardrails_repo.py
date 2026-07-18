"""Guardrails state machine + breaker queries for the live agent's brake.

Every state change is an ATOMIC conditional UPDATE (rows-affected election)
plus one appended ``AgentGuardrailEvent``; every read on the hot dispatch path
is a COLUMN select (never a cached ORM entity, which the dispatch loop's
long-lived Session would serve stale). The election on ``trip`` is the ONLY
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


def guardrails_mandate_ok(session: Session) -> tuple[bool, str]:
    """The mandate that real money may not dispatch with an unset breaker.

    Returns ``(True, "")`` only if ``max_daily_loss_usd``, ``max_trades_per_day``
    AND ``max_drawdown_usd`` are ALL set and the brake state is 'ok'. Otherwise
    refuses, naming the FIRST failing item so a misconfig reads as one concrete
    cause (mirrors ``settings.real_money_limits_ok``). ``loss_streak_halt`` is
    optional and never part of the mandate.
    """
    g = load_guardrails(session)
    if g.max_daily_loss_usd is None:
        return False, "max_daily_loss_usd is not set"
    if g.max_trades_per_day is None:
        return False, "max_trades_per_day is not set"
    if g.max_drawdown_usd is None:
        return False, "max_drawdown_usd is not set"
    if g.state != "ok":
        return False, f"guardrails state is {g.state}"
    return True, ""

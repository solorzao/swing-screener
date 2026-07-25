"""The brake's cockpit surface: GET/POST ``/api/guardrails`` and the trip-aware
``/api/disarm`` routing (Task 15).

The load-bearing properties proven here:

* READ-ONLY GET: the state snapshot, the live breach evaluation and the event
  history all come off ``peek_guardrails`` + the PURE breaker half, so a poll on a
  virgin table under a read-only DB grant answers 200 and attempts NOT ONE write
  (the G7 pin -- these endpoints are the first cockpit WRITERS of this table, so
  the read half has to stay provably clean).
* WRITES GO THROUGH THE STATE MACHINE: edit / halt / clear_halt / clear route to
  ``guardrails_repo`` verbatim (they seed, they audit, they commit atomically) and
  a DB failure on the PRIMARY state write PROPAGATES to the app-level 503 handler
  -- never a silent 200 that tells the operator the brake moved when it did not.
* VENUE-TOUCHING ACTIONS ARE SINGLE-FLIGHT: HALT runs the protective sweep, so it
  takes the SAME ``disarm_lock`` /api/disarm holds; the loser 409s before the
  factory resolves.
* TRIP-AWARE DISARM: a tripped book with an unfinished sweep routes through
  ``resume_incomplete_sweep`` so both processes share the ``guardrail-{trip_id}``
  client_order_ids (the venue's duplicate-ID rejection collapses the race) and the
  cockpit's run finally flips ``sweep_state`` to 'complete'. An untripped book is
  byte-identical to the pre-Task-15 endpoint.

All sqlite + FakeBroker -- no venue, no network.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import create_app
from swing_screener.db import guardrails_repo as gr
from swing_screener.db.models import AgentGuardrailEvent, AgentGuardrails, DisarmEvent
from swing_screener.db.session import get_engine
from swing_screener.pipeline.broker import FakeBroker
from tests.cockpit.conftest import (
    _broker_app,
    _db_url,
    _disarm_broker,
    _exec_log,
    _HDR,
    _kill_sell_legs,
    _nonce_of,
    _recorded_stop_row,
)


STATE_KEYS = {"state", "max_daily_loss_usd", "max_trades_per_day", "max_drawdown_usd",
              "loss_streak_halt", "hwm_anchor_date", "hwm_baseline_usd", "trip_id",
              "trip_reason", "sweep_state"}
GET_KEYS = STATE_KEYS | {"current_breach", "events"}
EVENT_KEYS = {"id", "kind", "breaker", "reason", "source", "created_at"}
SWEEP_KEYS = {"ran", "detail", "cancelled", "sells_kept", "stops_restored",
              "unprotected"}
DISARM_KEYS = {"dry_run", "cancelled", "sells_kept", "stops_restored", "unprotected"}
RESUME_KEYS = DISARM_KEYS | {"mode", "trip_id", "sweep_state", "detail"}


@contextmanager
def _deny_writes(*tables: str) -> Iterator[list[str]]:
    """Simulate a read-only DB grant: every INSERT/UPDATE touching ``tables`` raises.

    Task 12's listener idiom (tests/cockpit/test_safety_router.py), with ONE
    deliberate change: the refusal is an ``OperationalError`` -- a real
    ``SQLAlchemyError``, which is what a denied write actually raises through the
    driver -- so the endpoints' 503-never-200 posture is exercised end to end. A
    bare ``PermissionError`` would escape SQLAlchemy's wrapping and 500, which
    would test the harness rather than the app. Listens on the Engine CLASS
    because ``create_app`` builds its own engine from the URL. Yields the list of
    attempted statements (empty = nothing tried)."""
    attempted: list[str] = []

    def _guard(conn: Any, cursor: Any, statement: str, parameters: Any,
               context: Any, executemany: bool) -> None:
        sql = " ".join(statement.split()).lower()
        if sql.startswith(("insert into", "update")) and any(t in sql for t in tables):
            attempted.append(sql)
            raise OperationalError(statement, {}, Exception("no write grant"))

    event.listen(Engine, "before_cursor_execute", _guard)
    try:
        yield attempted
    finally:
        event.remove(Engine, "before_cursor_execute", _guard)


def _live_order(session: Session) -> None:
    """One live ExecutionLog on TODAY's run_date -- the max_trades_per_day input
    (``latest_run_date`` is None with no Signal rows, so the endpoint's day key
    falls back to ``date.today()``)."""
    session.add(_exec_log(run_date=date.today(), created_date=date.today(),
                          account="live", mode="live", status="submitted_live"))
    session.commit()


# --- GET /api/guardrails --------------------------------------------------------------


def test_get_returns_state_breach_and_history(tmp_path: Path) -> None:
    """One DB-only read: the full state snapshot, the LIVE breach evaluation, and the
    last events newest-first. ``current_breach`` is the state-blind pure read -- it
    answers "is a breaker breached RIGHT NOW" regardless of state, which is what lets
    the Task-16 clear dialog warn that a clear will simply re-trip."""
    client, engine, _calls = _broker_app(tmp_path, None)

    body = client.get("/api/guardrails").json()
    assert set(body) == GET_KEYS
    assert body["state"] == "ok"
    assert body["current_breach"] is None       # no breaker set -> nothing to breach
    assert body["events"] == []

    with Session(engine) as s:
        gr.edit_limits(s, source="test", max_daily_loss_usd=500.0,
                       max_trades_per_day=1, max_drawdown_usd=1_000.0,
                       hwm_anchor_date=date(2026, 7, 1), hwm_baseline_usd=250.0)
        _live_order(s)                          # 1 live order today -> 1 >= 1
        assert gr.halt(s, source="test") is True

    body = client.get("/api/guardrails").json()
    assert body["state"] == "halted"
    assert body["max_daily_loss_usd"] == 500.0
    assert body["max_trades_per_day"] == 1
    assert body["max_drawdown_usd"] == 1_000.0
    assert body["loss_streak_halt"] is None
    assert body["hwm_anchor_date"] == "2026-07-01"
    assert body["hwm_baseline_usd"] == 250.0
    assert body["trip_id"] is None and body["trip_reason"] is None
    assert body["sweep_state"] is None
    # the breaker reason string is guardrails_repo's, verbatim -- the same text
    # execution logs and trip_reason carries; a surface never re-words it.
    assert body["current_breach"] == {"breaker": "max_trades_per_day",
                                      "reason": "max trades/day: 1 >= 1"}
    kinds = [e["kind"] for e in body["events"]]
    assert kinds == ["halt", "edit"]            # newest FIRST
    assert all(set(e) == EVENT_KEYS for e in body["events"])
    assert body["events"][0]["source"] == "test"
    assert body["events"][0]["created_at"].endswith("+00:00")


def test_get_never_writes_on_virgin_table(tmp_path: Path) -> None:
    """The G7 pin: a virgin ``agent_guardrails`` table AND no write grant.

    The panel's own poll must answer 200 with the honest default -- NOT ONE
    INSERT/UPDATE even attempted. These endpoints are the first cockpit WRITERS of
    this table, so the read half has to stay provably clean: ``peek_guardrails``
    (never the seeding ``load``) plus the PURE breaker half, which is why
    ``current_breach`` costs zero queries when no breaker is set."""
    client, engine, _calls = _broker_app(tmp_path, FakeBroker())
    with _deny_writes("agent_guardrails", "agent_guardrail_events") as attempted:
        r = client.get("/api/guardrails")

    assert attempted == []
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "ok"                 # the default, not a seeded row
    assert body["current_breach"] is None
    assert body["events"] == []
    with Session(engine) as s:
        assert s.query(AgentGuardrails).count() == 0   # still virgin


# --- POST /api/guardrails: edit -------------------------------------------------------


def test_post_edit_appends_event_and_updates(tmp_path: Path) -> None:
    """The edit action routes to ``guardrails_repo.edit_limits`` -- the whitelist +
    the audited event + one atomic commit -- and answers the fresh snapshot. Only the
    keys the body actually CARRIED move (an omitted key is untouched; an explicit
    null UNSETS, which is how a breaker is disarmed)."""
    client, engine, _calls = _broker_app(tmp_path, None)
    assert client.post("/api/guardrails", json={"action": "edit"}).status_code == 403
    nonce = _nonce_of(client.app)

    r = client.post("/api/guardrails", headers=_HDR, json={
        "action": "edit", "max_daily_loss_usd": 500.0, "max_trades_per_day": 3,
        "loss_streak_halt": 4})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == STATE_KEYS | {"action", "current_breach"}
    assert body["action"] == "edit"
    assert body["max_daily_loss_usd"] == 500.0
    assert body["max_trades_per_day"] == 3
    assert body["loss_streak_halt"] == 4
    assert body["max_drawdown_usd"] is None      # untouched
    assert body["state"] == "ok"
    assert nonce.value == 1                      # other windows wake on a brake edit

    # an explicit null UNSETS one breaker and leaves the rest alone
    r = client.post("/api/guardrails", headers=_HDR,
                    json={"action": "edit", "loss_streak_halt": None})
    assert r.status_code == 200
    assert r.json()["loss_streak_halt"] is None
    assert r.json()["max_trades_per_day"] == 3

    with Session(engine) as s:
        row = s.query(AgentGuardrails).one()
        assert (row.max_daily_loss_usd, row.max_trades_per_day) == (500.0, 3)
        assert row.loss_streak_halt is None
        events = s.query(AgentGuardrailEvent).order_by(AgentGuardrailEvent.id).all()
        assert [e.kind for e in events] == ["edit", "edit"]
        assert all(e.source == "cockpit" for e in events)
        assert '"max_daily_loss_usd": 500.0' in events[0].values_json


def test_post_edit_rejects_unknown_key_and_nonpositive(tmp_path: Path) -> None:
    """The repo whitelist is THE gate, and its ValueError is the operator's message:
    a state column can never ride an edit (that is what makes "editing a cap while
    tripped leaves the brake tripped" a guarantee), and a zero/negative breaker --
    which would trip on the first close -- is refused. Both are 422s, and NOTHING is
    written on either."""
    client, engine, _calls = _broker_app(tmp_path, None)

    r = client.post("/api/guardrails", headers=_HDR,
                    json={"action": "edit", "state": "ok", "trip_id": 7})
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "edit_limits: not an editable limit column: state, trip_id")

    r = client.post("/api/guardrails", headers=_HDR,
                    json={"action": "edit", "max_daily_loss_usd": -5.0})
    assert r.status_code == 422
    assert "must be a positive number or None" in r.json()["detail"]

    r = client.post("/api/guardrails", headers=_HDR,
                    json={"action": "edit", "max_trades_per_day": 0})
    assert r.status_code == 422

    r = client.post("/api/guardrails", headers=_HDR, json={"action": "edit"})
    assert r.status_code == 422                  # no limits passed
    assert _nonce_of(client.app).value == 0

    with Session(engine) as s:
        assert s.query(AgentGuardrailEvent).count() == 0


def test_post_edit_db_failure_is_503_never_200(tmp_path: Path) -> None:
    """G7 posture, stated as a rule: the PRIMARY state write lets ``SQLAlchemyError``
    propagate to the app-level handler. A cockpit that answered 200 while the UPDATE
    was refused would tell the operator the brake moved when it did not -- the exact
    failure mode a brake cannot have. The write IS attempted (this is not a
    pre-flight refusal) and the wake nonce never bumps."""
    client, engine, _calls = _broker_app(tmp_path, None)
    with Session(engine) as s:                   # seed so the denied statement is the UPDATE
        gr.edit_limits(s, source="test", max_trades_per_day=2)

    with _deny_writes("agent_guardrails") as attempted:
        r = client.post("/api/guardrails", headers=_HDR,
                        json={"action": "edit", "max_trades_per_day": 9})

    assert r.status_code == 503
    assert r.json() == {"detail": "database error (OperationalError)"}
    assert attempted and attempted[0].startswith("update")
    assert _nonce_of(client.app).value == 0
    with Session(engine) as s:
        assert s.query(AgentGuardrails).one().max_trades_per_day == 2   # unchanged


# --- POST /api/guardrails: halt -------------------------------------------------------


def test_post_halt_runs_sweep_under_lock(tmp_path: Path) -> None:
    """HALT moves VENUE state (it runs the protective sweep), so it takes the same
    single-flight lock /api/disarm holds -- the loser 409s before the factory even
    resolves. The winner: the DB brake lands FIRST (persist-first), then the sweep
    pulls entries and restores the dead stop under a ``halt-cockpit-`` key, a
    DisarmEvent(reason='halt') reaches the Auditor, and the wake nonce bumps."""
    broker = _disarm_broker()
    _kill_sell_legs(broker)                      # NVDA's stop leg is dead
    client, engine, calls = _broker_app(tmp_path, broker)
    with Session(engine) as s:
        s.add(_recorded_stop_row("NVDA", 95.0))
        s.commit()

    lock = client.app.state.disarm_lock  # type: ignore[attr-defined]
    assert lock.acquire(blocking=False)
    try:
        r = client.post("/api/guardrails", headers=_HDR, json={"action": "halt"})
        assert r.status_code == 409
        assert r.json()["detail"] == "guardrail action already in flight"
        assert calls["n"] == 0                   # rejected before the factory
        assert len(broker.list_open_orders()) == 1   # venue untouched
    finally:
        lock.release()

    nonce = _nonce_of(client.app)
    r = client.post("/api/guardrails", headers=_HDR, json={"action": "halt"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == STATE_KEYS | {"action", "dry_run", "sweep"}
    assert body["state"] == "halted"
    assert body["dry_run"] is False
    sweep = body["sweep"]
    assert set(sweep) == SWEEP_KEYS
    assert sweep["ran"] is True
    assert sweep["cancelled"] == [{"symbol": "AMD", "broker_order_id": "fake-0"}]
    assert sweep["stops_restored"] == ["NVDA"]
    assert sweep["unprotected"] == []
    restored = broker.submitted_specs[-1]
    assert restored.client_order_id.startswith("disarm-stop-NVDA-halt-cockpit-")
    assert restored.stop_price == 95.0           # COPIED from the ticket
    assert nonce.value == 1

    with Session(engine) as s:
        assert s.query(AgentGuardrails).one().state == "halted"
        assert [e.kind for e in s.query(AgentGuardrailEvent).all()] == ["halt"]
        ev = s.query(DisarmEvent).one()
        assert (ev.reason, ev.orders_cancelled) == ("halt", 1)

    # a second HALT has nothing to halt: 409 naming the state, venue untouched.
    n_specs = len(broker.submitted_specs)
    r = client.post("/api/guardrails", headers=_HDR, json={"action": "halt"})
    assert r.status_code == 409
    assert r.json()["detail"] == "state is not ok -- nothing to halt (state is halted)"
    assert len(broker.submitted_specs) == n_specs


def test_post_halt_without_broker_still_halts(tmp_path: Path) -> None:
    """No broker configured is a STATE, not a failure: the DB brake is the truth
    (it blocks every dispatch path), and the venue sweep is best-effort exactly as
    ``respond_to_trip``'s broker-None path is. The halt STANDS and the response says
    plainly that no sweep ran -- no DisarmEvent, because nothing moved."""
    client, engine, _calls = _broker_app(tmp_path, None)
    r = client.post("/api/guardrails", headers=_HDR, json={"action": "halt"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "halted"
    assert body["sweep"] == {"ran": False, "detail": "no broker configured",
                             "cancelled": [], "sells_kept": 0,
                             "stops_restored": [], "unprotected": []}
    assert _nonce_of(client.app).value == 1
    with Session(engine) as s:
        assert s.query(AgentGuardrails).one().state == "halted"
        assert s.query(DisarmEvent).count() == 0


def test_post_halt_engages_the_brake_before_touching_the_broker(
    tmp_path: Path,
) -> None:
    """ORDER PIN: the DB brake lands BEFORE the broker is resolved, so a venue that
    is down or whose credentials no longer resolve can never stop an operator from
    stopping the machine. The failed sweep is still LOUD (503, exception class only
    -- the message can embed the venue host), and the brake is durable behind it:
    the state reads 'halted' and pressing HALT again answers 409 naming that state,
    so the 503 can never be misread as a brake that failed to engage."""

    def exploding() -> FakeBroker | None:
        raise RuntimeError("secret-venue-host.alpaca.markets credential expired")

    url = _db_url(tmp_path)
    engine = get_engine(url)
    client = TestClient(create_app(url, edge_dir=tmp_path,
                                   latest_closes_fn=lambda tickers: {},
                                   broker_factory=exploding))

    r = client.post("/api/guardrails", headers=_HDR, json={"action": "halt"})
    assert r.status_code == 503
    assert r.json()["detail"] == "broker error (RuntimeError)"
    assert "secret-venue-host" not in r.text     # leak posture: class only

    assert client.get("/api/guardrails").json()["state"] == "halted"
    assert _nonce_of(client.app).value == 1      # the brake moved: wake every window
    with Session(engine) as s:
        assert s.query(AgentGuardrails).one().state == "halted"
        assert [e.kind for e in s.query(AgentGuardrailEvent).all()] == ["halt"]
        assert s.query(DisarmEvent).count() == 0   # no sweep ran, nothing to journal
    r = client.post("/api/guardrails", headers=_HDR, json={"action": "halt"})
    assert r.status_code == 409
    assert r.json()["detail"] == "state is not ok -- nothing to halt (state is halted)"


def test_post_halt_dry_run_previews_without_state_change(tmp_path: Path) -> None:
    """``dry_run=1`` is the hold-to-confirm preview: it says what the sweep WOULD do
    and changes NOTHING -- no state transition, no event row, not even a seeded
    brake row, and the venue byte-for-byte untouched."""
    broker = _disarm_broker()
    _kill_sell_legs(broker)
    client, engine, _calls = _broker_app(tmp_path, broker)
    with Session(engine) as s:
        s.add(_recorded_stop_row("NVDA", 95.0))
        s.commit()
    n_specs = len(broker.submitted_specs)

    r = client.post("/api/guardrails?dry_run=1", headers=_HDR, json={"action": "halt"})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["state"] == "ok"                 # unchanged
    assert body["sweep"]["ran"] is False
    assert body["sweep"]["cancelled"] == [{"symbol": "AMD",
                                           "broker_order_id": "fake-0"}]
    assert body["sweep"]["stops_restored"] == ["NVDA"]

    assert [o.symbol for o in broker.list_open_orders()] == ["AMD"]   # still resting
    assert len(broker.submitted_specs) == n_specs
    assert _nonce_of(client.app).value == 0
    with Session(engine) as s:
        assert s.query(AgentGuardrails).count() == 0     # not even seeded
        assert s.query(AgentGuardrailEvent).count() == 0
        assert s.query(DisarmEvent).count() == 0


# --- POST /api/guardrails: clear_halt / clear_trip ------------------------------------


def test_post_clear_halt(tmp_path: Path) -> None:
    """Releasing a manual HALT is DB-only (nothing at the venue to undo). A
    clear with no halt in force is a 409 NAMING the actual state -- the operator's
    screen may simply be stale."""
    client, engine, _calls = _broker_app(tmp_path, None)
    r = client.post("/api/guardrails", headers=_HDR, json={"action": "clear_halt"})
    assert r.status_code == 409
    assert r.json()["detail"] == "state is ok -- there is no HALT to clear"

    with Session(engine) as s:
        assert gr.halt(s, source="test") is True
    r = client.post("/api/guardrails", headers=_HDR, json={"action": "clear_halt"})
    assert r.status_code == 200
    assert set(r.json()) == STATE_KEYS | {"action"}
    assert r.json()["state"] == "ok"
    assert _nonce_of(client.app).value == 1

    # a TRIP never clears through here -- that is clear_trip's acknowledged path.
    with Session(engine) as s:
        assert gr.trip(s, breaker="max_drawdown_usd", reason="dd", source="screen")
    r = client.post("/api/guardrails", headers=_HDR, json={"action": "clear_halt"})
    assert r.status_code == 409
    assert r.json()["detail"] == "state is tripped -- there is no HALT to clear"


def test_post_clear_trip_requires_matching_ack(tmp_path: Path) -> None:
    """The stale-cockpit-screen guard: a clear must acknowledge THE trip the
    operator actually read. A stale id matches nothing and the brake STAYS ON."""
    client, engine, _calls = _broker_app(tmp_path, None)
    with Session(engine) as s:
        trip_id = gr.trip(s, breaker="max_drawdown_usd", reason="dd breach",
                          source="screen")
    assert trip_id is not None

    r = client.post("/api/guardrails", headers=_HDR, json={"action": "clear_trip"})
    assert r.status_code == 422
    assert r.json()["detail"] == "clear_trip requires ack_trip_id"

    r = client.post("/api/guardrails", headers=_HDR,
                    json={"action": "clear_trip", "ack_trip_id": trip_id + 99})
    assert r.status_code == 409
    assert r.json()["detail"] == "trip id is stale or state is not tripped"
    assert client.get("/api/guardrails").json()["state"] == "tripped"
    assert _nonce_of(client.app).value == 0

    r = client.post("/api/guardrails", headers=_HDR,
                    json={"action": "clear_trip", "ack_trip_id": trip_id})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == STATE_KEYS | {"action", "still_breached"}
    assert body["state"] == "ok"
    assert body["trip_id"] is None and body["sweep_state"] is None
    assert body["still_breached"] is None        # no breaker set -> nothing breached
    assert _nonce_of(client.app).value == 1


def test_post_clear_trip_reports_still_breached(tmp_path: Path) -> None:
    """The Task-11 clear-with-active-breach UX: the cumulative breakers re-fire on
    the next hourly consult if the breach is still real (by design -- the drawdown
    IS still there; the remedy is the explicit anchor reset). The response says so
    the moment the clear lands, so the panel can warn instead of letting the
    operator discover it via a second trip email."""
    client, engine, _calls = _broker_app(tmp_path, None)
    with Session(engine) as s:
        gr.edit_limits(s, source="test", max_trades_per_day=1)
        _live_order(s)
        trip_id = gr.trip(s, breaker="max_trades_per_day",
                          reason="max trades/day: 1 >= 1", source="digest")
    assert trip_id is not None

    r = client.post("/api/guardrails", headers=_HDR,
                    json={"action": "clear_trip", "ack_trip_id": trip_id})
    assert r.status_code == 200
    assert r.json()["state"] == "ok"
    assert r.json()["still_breached"] == {"breaker": "max_trades_per_day",
                                          "reason": "max trades/day: 1 >= 1"}


# --- trip-aware /api/disarm -----------------------------------------------------------


def test_disarm_routes_through_resume_when_tripped(tmp_path: Path) -> None:
    """The Task-6 red-team closure. A tripped book whose sweep never finished is
    swept through ``resume_incomplete_sweep``, NOT the raw cockpit sweep: the stop
    re-submit carries the ``guardrail-{trip_id}`` client_order_id both processes
    derive from the trip, so the venue's duplicate-ID rejection collapses a
    concurrent sweep (two live GTC sell stops on a margin account would close the
    position and then SHORT it). It also finishes the bookkeeping -- ``sweep_state``
    flips to 'complete', un-sticking the TRIPPED/SWEEP-PARTIAL banner -- and the
    DisarmEvent is the resume path's SANCTIONED ``guardrail:<breaker>``, never a
    second 'cockpit' row the Auditor would read as an unexplained disarm."""
    broker = _disarm_broker()
    _kill_sell_legs(broker)
    client, engine, _calls = _broker_app(tmp_path, broker)
    with Session(engine) as s:
        s.add(_recorded_stop_row("NVDA", 95.0))
        s.commit()
        trip_id = gr.trip(s, breaker="max_drawdown_usd", reason="dd breach",
                          source="screen")
        assert trip_id is not None
        gr.record_sweep_outcome(s, trip_id=trip_id, outcome="partial",
                                detail="venue died", source="screen")

    preview = client.post("/api/disarm?dry_run=1", headers=_HDR)
    assert preview.status_code == 200
    pbody = preview.json()
    assert set(pbody) == RESUME_KEYS
    assert pbody["mode"] == "guardrail-resume-preview"
    assert pbody["trip_id"] == trip_id
    assert pbody["cancelled"] == [{"symbol": "AMD", "broker_order_id": "fake-0"}]
    assert pbody["stops_restored"] == ["NVDA"]
    assert pbody["sweep_state"] == "partial"          # a preview changes nothing
    assert [o.symbol for o in broker.list_open_orders()] == ["AMD"]

    r = client.post("/api/disarm", headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == RESUME_KEYS
    assert body["mode"] == "guardrail-resume"
    assert body["trip_id"] == trip_id
    assert body["sweep_state"] == "complete"
    assert "1 entry order(s) cancelled" in body["detail"]

    restored = broker.submitted_specs[-1]
    assert restored.client_order_id == f"disarm-stop-NVDA-guardrail-{trip_id}"
    assert restored.stop_price == 95.0
    assert [o for o in broker.list_open_orders() if o.side == "buy"] == []
    with Session(engine) as s:
        row = s.query(AgentGuardrails).one()
        assert (row.state, row.sweep_state) == ("tripped", "complete")
        ev = s.query(DisarmEvent).one()          # the resume path's row, and ONLY it
        assert ev.reason == "guardrail:max_drawdown_usd"


def test_disarm_untripped_behavior_unchanged(tmp_path: Path) -> None:
    """The pin on the other side: anything that is not a tripped-with-unfinished-sweep
    book takes the pre-Task-15 path byte-identically -- the closed 5-key wire shape,
    the ``cockpit-`` key suffix, the plain ``cockpit`` DisarmEvent. A tripped book
    whose sweep is already 'complete' is included: there is nothing to resume."""
    broker = _disarm_broker()
    _kill_sell_legs(broker)
    client, engine, _calls = _broker_app(tmp_path, broker)
    with Session(engine) as s:
        s.add(_recorded_stop_row("NVDA", 95.0))
        s.commit()
        trip_id = gr.trip(s, breaker="max_drawdown_usd", reason="dd", source="screen")
        assert trip_id is not None
        gr.record_sweep_outcome(s, trip_id=trip_id, outcome="complete",
                                detail="done", source="screen")

    r = client.post("/api/disarm", headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == DISARM_KEYS              # no ``mode`` key at all
    assert body["stops_restored"] == ["NVDA"]
    assert broker.submitted_specs[-1].client_order_id.startswith(
        "disarm-stop-NVDA-cockpit-")
    with Session(engine) as s:
        assert s.query(DisarmEvent).one().reason == "cockpit"


@pytest.mark.parametrize("state", ["ok", "halted"])
def test_disarm_untripped_states_take_the_raw_sweep(tmp_path: Path, state: str) -> None:
    """Same pin across the two non-tripped states: a HALT is not a trip, so a halted
    book's DISARM is still the plain cockpit sweep (there is no trip id to key on)."""
    broker = _disarm_broker()
    client, engine, _calls = _broker_app(tmp_path, broker)
    if state == "halted":
        with Session(engine) as s:
            assert gr.halt(s, source="test") is True
    r = client.post("/api/disarm", headers=_HDR)
    assert r.status_code == 200
    assert set(r.json()) == DISARM_KEYS

"""Safety-router endpoints (split from test_api.py): /api/gate, POST
/api/disarm, and GET /api/execution/safety."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from swing_screener.analytics.calibration import _CLUSTER_FLOOR, MIN_LEADERBOARD_N
from swing_screener.cockpit.api import create_app
from swing_screener.db import guardrails_repo as gr
from swing_screener.db.models import AgentGuardrails
from swing_screener.db.session import get_engine
from swing_screener.pipeline.broker import BrokerAccount, BrokerOrderSpec, FakeBroker
from tests.cockpit.conftest import (
    _broker_app,
    _call,
    _db_url,
    _disarm_broker,
    _HDR,
    _kill_sell_legs,
    _nonce_of,
    _recorded_stop_row,
)


@contextmanager
def _deny_writes(table: str) -> Iterator[list[str]]:
    """Simulate a read-only DB grant: every INSERT/UPDATE touching ``table`` raises.

    Listens on the Engine CLASS, not one instance, because ``create_app`` builds its
    own engine from the URL -- an instance listener would miss exactly the writes
    under test. Yields the list of attempted statements (empty = nothing tried)."""
    attempted: list[str] = []

    def _guard(conn: Any, cursor: Any, statement: str, parameters: Any,
               context: Any, executemany: bool) -> None:
        sql = " ".join(statement.split()).lower()
        if sql.startswith(("insert into", "update")) and table in sql:
            attempted.append(sql)
            raise PermissionError(f"no write grant on {table}")

    event.listen(Engine, "before_cursor_execute", _guard)
    try:
        yield attempted
    finally:
        event.remove(Engine, "before_cursor_execute", _guard)


# --- /api/gate ------------------------------------------------------------------------

def test_gate_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """{"ready", "countdown", "execution_mode", "analyst_spend_today_usd",
    "broker_configured"}: the report comes from pipeline.autonomy verbatim (missing
    verdicts sidecars read as not-ready, never an error), the mode from live settings
    at request time, and the spend sums est_cost_usd over TODAY's AnalystCall rows
    (NULL costs -- deterministic-path rows -- count 0; yesterday's spend never bleeds
    in). ``broker_configured`` is settings TRUTHINESS (is SWING_BROKER set), never
    connectivity -- it rides the gate so the always-visible masthead needs no extra
    poll."""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "manual")
    monkeypatch.delenv("SWING_BROKER", raising=False)
    url = _db_url(tmp_path)
    engine = get_engine(url)
    today = date.today()
    with Session(engine) as s:
        s.add_all([_call(today, 0.12), _call(today, 0.30), _call(today, None),
                   _call(today - timedelta(days=1), 5.0)])
        s.commit()
    r = TestClient(create_app(url, edge_dir=tmp_path)).get("/api/gate")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ready", "countdown", "execution_mode",
                         "analyst_spend_today_usd", "broker_configured", "brake_state"}
    assert body["ready"] is False  # no verdicts sidecars in tmp_path, no scored calls
    # gate_countdown VERBATIM (line format pinned by tests/pipeline/
    # test_autonomy_countdown.py); denominators are the live floor constants.
    assert body["countdown"] == "\n".join(
        f"{pt}: 0/{MIN_LEADERBOARD_N} high, 0/{MIN_LEADERBOARD_N} low, "
        f"0/{_CLUSTER_FLOOR} tickers"
        for pt in ("continuation", "reversal"))
    assert body["execution_mode"] == "manual"
    assert body["analyst_spend_today_usd"] == pytest.approx(0.42)
    assert body["broker_configured"] is False


def test_gate_carries_broker_configured_from_settings_truthiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SWING_BROKER set -> True, without any venue call: the flag is read from the
    settings snapshot (truthiness), NEVER from connectivity -- an unreachable venue
    still shows a configured broker (DISARM enablement keys on this)."""
    monkeypatch.setenv("SWING_BROKER", "alpaca")
    url = _db_url(tmp_path)
    get_engine(url)
    r = TestClient(create_app(url, edge_dir=tmp_path)).get("/api/gate")
    assert r.status_code == 200
    assert r.json()["broker_configured"] is True


def test_gate_carries_brake_state(tmp_path: Path) -> None:
    """The masthead chip's data source: the brake state rides the already-polled gate
    (one extra column select, no second poll and no venue call). It is ``g.state``
    rendered DIRECTLY -- never a consult verdict -- and it is read fresh per request,
    so a HALT committed by ANOTHER process (a cockpit action, an Azure job's trip)
    shows on the very next poll rather than out of a cached entity."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    client = TestClient(create_app(url, edge_dir=tmp_path))

    assert client.get("/api/gate").json()["brake_state"] == "ok"
    with Session(engine) as s:  # a SECOND session, as a cockpit action would be
        assert gr.halt(s, source="test") is True
    assert client.get("/api/gate").json()["brake_state"] == "halted"

    with Session(engine) as s:
        assert gr.clear_halt(s, source="test") is True
        assert gr.trip(s, breaker="max_trades_per_day", reason="cap", source="test")
    assert client.get("/api/gate").json()["brake_state"] == "tripped"


# ---- POST /api/disarm + GET /api/execution/safety (Task 9) ----

DISARM_KEYS = {"dry_run", "cancelled", "sells_kept", "stops_restored", "unprotected"}
SAFETY_KEYS = {"broker_configured", "mode", "env_scope", "locks", "caps_mandate",
               "guardrails", "preflight", "bracket_shield"}
GUARDRAIL_KEYS = {"ok", "reason", "state", "sweep_state"}
LOCK_KEYS = {"mode_is_live", "allow_real_money", "gate_ready"}
CHECK_KEYS = {"name", "ok", "detail", "critical"}


def test_disarm_requires_the_cockpit_header(tmp_path: Path) -> None:
    broker = _disarm_broker()
    client, _engine, _calls = _broker_app(tmp_path, broker)
    assert client.post("/api/disarm").status_code == 403
    assert len(broker.list_open_orders()) == 3  # the guard ran before any cancel


def test_disarm_409_when_no_broker_configured(tmp_path: Path) -> None:
    """A None factory (the default local setup) is a STATE, not a crash: 409 with
    a fixed detail -- there is nothing at a venue to disarm."""
    client, _engine, _calls = _broker_app(tmp_path, None)
    r = client.post("/api/disarm", headers=_HDR)
    assert r.status_code == 409
    assert r.json()["detail"] == "no broker configured"


def test_disarm_dry_run_cancels_nothing_and_previews_everything(
    tmp_path: Path,
) -> None:
    """dry_run=1 answers the full preview -- what would be cancelled, the sells
    kept, the stop that would be restored -- while the venue stays byte-for-byte
    untouched (the FakeBroker's orders and spec count are the proof)."""
    broker = _disarm_broker()
    _kill_sell_legs(broker)  # NVDA's stop leg is dead -> the restore path previews
    n_specs = len(broker.submitted_specs)
    client, engine, _calls = _broker_app(tmp_path, broker)
    with Session(engine) as s:
        s.add(_recorded_stop_row("NVDA", 95.0))
        s.commit()
    r = client.post("/api/disarm?dry_run=1", headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == DISARM_KEYS
    assert body["dry_run"] is True
    assert body["cancelled"] == [{"symbol": "AMD", "broker_order_id": "fake-0"}]
    assert body["sells_kept"] == 0  # the legs are dead; nothing sell-side survives
    assert body["stops_restored"] == ["NVDA"]  # the hold preview names the symbol
    assert body["unprotected"] == []
    # the venue: AMD entry still OPEN, nothing submitted, the position untouched.
    assert [o.symbol for o in broker.list_open_orders() if o.side == "buy"] == ["AMD"]
    assert len(broker.submitted_specs) == n_specs
    assert len(broker.get_positions()) == 1


def test_disarm_real_run_cancels_buys_only_and_keeps_sells(tmp_path: Path) -> None:
    """The real run: entry-side buys pulled, BOTH venue-held sell legs kept (the
    2026-07-04 bug this module exists to prevent), the position NEVER closed."""
    broker = _disarm_broker()
    client, _engine, _calls = _broker_app(tmp_path, broker)
    r = client.post("/api/disarm", headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is False
    assert body["cancelled"] == [{"symbol": "AMD", "broker_order_id": "fake-0"}]
    assert body["sells_kept"] == 2  # NVDA's protective stop + target legs
    assert body["stops_restored"] == []  # the stop leg is alive; nothing to restore
    assert body["unprotected"] == []
    open_orders = broker.list_open_orders()
    assert [o for o in open_orders if o.side == "buy"] == []      # entries pulled
    assert sorted(o.order_type for o in open_orders
                  if o.side == "sell") == ["limit", "stop"]        # legs SURVIVE
    assert len(broker.get_positions()) == 1                        # never closed


def test_disarm_real_run_records_a_disarm_event(tmp_path: Path) -> None:
    """A REAL disarm persists a DisarmEvent (Auditor input); a dry run records nothing."""
    from swing_screener.db.models import DisarmEvent

    broker = _disarm_broker()
    client, engine, _calls = _broker_app(tmp_path, broker)
    assert client.post("/api/disarm?dry_run=true", headers=_HDR).status_code == 200
    with Session(engine) as s:
        assert s.query(DisarmEvent).count() == 0        # dry run records nothing
    assert client.post("/api/disarm", headers=_HDR).status_code == 200
    with Session(engine) as s:
        ev = s.query(DisarmEvent).one()
        assert ev.orders_cancelled == 1 and ev.reason == "cockpit"


def test_disarm_restores_dead_stop_at_the_recorded_level(tmp_path: Path) -> None:
    """A dead stop leg is re-submitted at the ExecutionLog ticket's RECORDED level
    -- COPIED, never computed (North Star #4) -- as a plain GTC stop covering the
    whole position, and the response's restored list names the symbol."""
    broker = _disarm_broker()
    _kill_sell_legs(broker)
    client, engine, _calls = _broker_app(tmp_path, broker)
    with Session(engine) as s:
        s.add(_recorded_stop_row("NVDA", 90.0))   # older ticket
        s.add(_recorded_stop_row("NVDA", 95.0))   # newest live ticket wins
        s.commit()
    r = client.post("/api/disarm", headers=_HDR)
    assert r.status_code == 200
    assert r.json()["stops_restored"] == ["NVDA"]
    restored = broker.submitted_specs[-1]
    assert restored.side == "sell"
    assert restored.order_type == "stop"
    assert restored.stop_price == 95.0        # COPIED from the ticket, not computed
    assert restored.qty == 8                  # covers the whole position
    assert restored.time_in_force == "gtc"    # protection must not expire at EOD
    assert restored.client_order_id.startswith("disarm-stop-NVDA-cockpit-")


def test_disarm_unprotected_is_loud_and_nothing_is_invented(tmp_path: Path) -> None:
    """No live stop and no recorded ticket level: the position is named in
    ``unprotected`` and LEFT ALONE -- no invented level, no auto-close."""
    broker = _disarm_broker()
    _kill_sell_legs(broker)
    client, _engine, _calls = _broker_app(tmp_path, broker)  # no ExecutionLog rows
    r = client.post("/api/disarm", headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["stops_restored"] == []
    assert body["unprotected"] == ["NVDA"]
    assert [o for o in broker.list_open_orders() if o.side == "sell"] == []
    assert len(broker.get_positions()) == 1


def test_disarm_real_run_invalidates_the_broker_snapshot(tmp_path: Path) -> None:
    """After a REAL run the cached venue snapshot is busted (the next read hits the
    factory again); a DRY run leaves the warm cache alone -- the factory-call
    counter is the observable."""
    broker = _disarm_broker()
    client, _engine, calls = _broker_app(tmp_path, broker)
    client.get("/api/positions")                      # primes the snapshot
    assert calls["n"] == 1
    client.post("/api/disarm?dry_run=1", headers=_HDR)
    assert calls["n"] == 2                            # the endpoint's live client
    client.get("/api/positions")
    assert calls["n"] == 2                            # dry run: cache still warm
    client.post("/api/disarm", headers=_HDR)
    assert calls["n"] == 3
    client.get("/api/positions")
    assert calls["n"] == 4                            # real run: snapshot re-read


def test_disarm_is_single_flight(tmp_path: Path) -> None:
    """Two overlapping real runs can both read the open-order list before either
    cancels, and differing per-second key_suffixes defeat the client_order_id
    dedup -- duplicate live GTC sell stops (2x the position). The lock is held
    deterministically here (it lives on app.state for exactly this): the second
    request 409s BEFORE resolving a live client, and the lock is released
    per-request so a later disarm proceeds."""
    broker = _disarm_broker()
    client, _engine, calls = _broker_app(tmp_path, broker)
    lock = client.app.state.disarm_lock  # type: ignore[attr-defined]
    assert lock.acquire(blocking=False)  # an in-flight disarm holds the lock
    try:
        r = client.post("/api/disarm", headers=_HDR)
        assert r.status_code == 409
        assert r.json()["detail"] == "disarm already in flight"
        assert calls["n"] == 0                       # rejected before the factory
        assert len(broker.list_open_orders()) == 3   # venue untouched
    finally:
        lock.release()
    assert client.post("/api/disarm", headers=_HDR).status_code == 200


def test_disarm_broker_failure_is_503_class_only_and_busts_the_snapshot(
    tmp_path: Path,
) -> None:
    """A mid-disarm venue failure: 503 whose detail is the exception CLASS only
    (the message can embed the venue host), AND the snapshot is still invalidated
    AND the post-action nonce still bumps -- both in the ``finally`` -- because a
    PARTIAL disarm may already have moved venue state: other windows must wake
    NOW, not at the 60s poll floor, on exactly the action where staleness is
    scariest; the factory-call counter is the invalidation observable (as in the
    invalidation test above). A SECOND disarm answers 503 again, never 409: the
    single-flight lock is released on the EXCEPTION path too (the outer
    ``finally``), so a failed run never wedges the endpoint shut."""

    class _CancelRefusedBroker(FakeBroker):
        def cancel_order(self, broker_order_id: str) -> None:
            raise RuntimeError("secret-venue-host.alpaca.markets refused the cancel")

    broker = _disarm_broker(_CancelRefusedBroker())
    client, _engine, calls = _broker_app(tmp_path, broker)
    nonce = _nonce_of(client.app)
    client.get("/api/positions")                      # primes the snapshot
    assert calls["n"] == 1
    r = client.post("/api/disarm", headers=_HDR)      # live client: n -> 2
    assert r.status_code == 503
    assert r.json()["detail"] == "broker error (RuntimeError)"
    assert "secret-venue-host" not in r.text          # leak posture: class only
    assert nonce.value == 1  # the partial run moved venue state: wake despite 503
    client.get("/api/positions")
    assert calls["n"] == 3                            # partial run STILL busted it
    # 503, NOT 409: the failed run released the lock (a success-only release
    # would leave it held and this request would read 'already in flight').
    assert client.post("/api/disarm", headers=_HDR).status_code == 503
    assert nonce.value == 2  # every real attempt that reached the venue wakes


def test_execution_safety_none_factory_is_200_never_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default local setup (no broker anywhere): 200 with broker_configured
    False, the preflight report in its None-broker shape (config NO-GO, explicit
    not-applicable lines), all three locks open, the caps mandate named, and an
    UNKNOWN bracket shield -- no-broker is a STATE, never a crash."""
    for var in ("SWING_BROKER", "SWING_EXECUTION_MODE", "SWING_MAX_DAILY_NOTIONAL",
                "SWING_MAX_DAILY_LOSS", "SWING_MAX_CONCURRENT",
                "SWING_BROKER_ALLOW_REAL_MONEY"):
        monkeypatch.delenv(var, raising=False)
    client, _engine, _calls = _broker_app(tmp_path, None)
    r = client.get("/api/execution/safety")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == SAFETY_KEYS
    assert body["broker_configured"] is False
    assert body["mode"] == "off"
    # the SAME honesty label /api/config carries -- one string, one meaning.
    assert body["env_scope"] == "this process — the Azure jobs run under their own env"
    assert set(body["locks"]) == LOCK_KEYS
    assert body["locks"] == {"mode_is_live": False, "allow_real_money": False,
                             "gate_ready": False}
    assert body["caps_mandate"]["ok"] is False
    assert body["caps_mandate"]["reason"] == "max_daily_notional is not set"
    pf = body["preflight"]
    assert pf["go"] is False
    checks = {c["name"]: c for c in pf["checks"]}
    assert all(set(c) == CHECK_KEYS for c in pf["checks"])
    assert checks["config"]["ok"] is False
    assert "no broker configured" in checks["config"]["detail"]
    for name in ("reachable", "funded"):
        assert checks[name]["ok"] is False
        assert checks[name]["detail"] == "not applicable -- no broker"
    assert checks["caps"]["ok"] is False  # the mandate line, evaluated for real
    # the brake line too -- it reads the DB, never the venue, so no broker is no
    # excuse; ADVISORY here because with no client real-vs-paper is UNKNOWN.
    assert checks["guardrails"]["ok"] is False
    assert checks["guardrails"]["critical"] is False
    shield = body["bracket_shield"]
    assert shield == {"known": False, "as_of": None, "positions": []}


def test_execution_safety_reports_locks_caps_and_bracket_shield(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The armed-paper picture: broker_configured from settings truthiness, the
    lock components rendered individually (paper -> mode_is_live False), the caps
    mandate green, preflight GO, and the bracket shield read from the SNAPSHOT --
    per venue position: armed (venue-held sell stop) / db-only (recorded level
    only) / unprotected (no level anywhere; never silently green)."""
    monkeypatch.setenv("SWING_BROKER", "alpaca")
    monkeypatch.setenv("SWING_EXECUTION_MODE", "paper")
    monkeypatch.setenv("SWING_MAX_DAILY_NOTIONAL", "10000")
    monkeypatch.setenv("SWING_MAX_DAILY_LOSS", "500")
    monkeypatch.setenv("SWING_MAX_CONCURRENT", "3")
    monkeypatch.delenv("SWING_BROKER_ALLOW_REAL_MONEY", raising=False)
    broker = FakeBroker()
    armed_entry = broker.submit_order(BrokerOrderSpec(
        client_order_id="armed", symbol="AMD", side="buy", qty=5,
        order_type="limit", limit_price=100.0, time_in_force="day",
        stop_loss=95.0, take_profit=110.0))
    broker.fill(armed_entry.broker_order_id, 100.0)     # venue-held legs LIVE
    for symbol in ("NVDA", "XYZY"):                     # plain fills: no legs
        plain = broker.submit_order(BrokerOrderSpec(
            client_order_id=f"plain-{symbol}", symbol=symbol, side="buy", qty=8,
            order_type="limit", limit_price=50.0, time_in_force="day"))
        broker.fill(plain.broker_order_id, 50.0)
    client, engine, _calls = _broker_app(tmp_path, broker)
    with Session(engine) as s:
        s.add(_recorded_stop_row("NVDA", 45.0))  # NVDA: recorded level -> db-only
        s.commit()                               # XYZY: nothing -> unprotected
    r = client.get("/api/execution/safety")
    assert r.status_code == 200
    body = r.json()
    assert body["broker_configured"] is True
    assert body["mode"] == "paper"
    assert body["locks"] == {"mode_is_live": False, "allow_real_money": False,
                             "gate_ready": False}
    assert body["caps_mandate"] == {"ok": True, "reason": ""}
    pf = body["preflight"]
    assert pf["go"] is True  # config/reachable/funded/caps all green (FakeBroker)
    checks = {c["name"]: c for c in pf["checks"]}
    assert checks["reachable"]["ok"] is True
    assert checks["funded"]["ok"] is True
    shield = body["bracket_shield"]
    assert shield["known"] is True
    assert datetime.fromisoformat(shield["as_of"])
    states = {row["symbol"]: row["state"] for row in shield["positions"]}
    assert states == {"AMD": "armed", "NVDA": "db-only", "XYZY": "unprotected"}
    assert all(row["qty"] > 0 for row in shield["positions"])


def test_safety_report_carries_guardrails_entry(tmp_path: Path) -> None:
    """The safety screen's brake row: the mandate verdict + its reason + the RAW state
    and sweep bookkeeping (state rendered DIRECTLY, never a consult verdict).

    Unlike the env-derived rows beside it, this one is a DB read -- the venue of record
    EVERY process shares -- so a trip landed by an Azure job shows here, and the
    ``env_scope: this process`` label never applies to it."""
    client, engine, _calls = _broker_app(tmp_path, FakeBroker())

    body = client.get("/api/execution/safety").json()
    assert set(body["guardrails"]) == GUARDRAIL_KEYS
    # the default row: breakers unset -> the mandate refuses, naming the FIRST gap.
    assert body["guardrails"] == {"ok": False, "reason": "max_daily_loss_usd is not set",
                                  "state": "ok", "sweep_state": None}

    with Session(engine) as s:
        gr.edit_limits(s, source="test", max_daily_loss_usd=500.0,
                       max_trades_per_day=3, max_drawdown_usd=1_000.0)
    assert client.get("/api/execution/safety").json()["guardrails"] == {
        "ok": True, "reason": "", "state": "ok", "sweep_state": None}

    with Session(engine) as s:  # a trip landed by ANOTHER process (the screen job)
        assert gr.trip(s, breaker="max_drawdown_usd", reason="dd breach", source="screen")
    assert client.get("/api/execution/safety").json()["guardrails"] == {
        "ok": False, "reason": "guardrails state is tripped",
        "state": "tripped", "sweep_state": "pending"}


def test_gate_and_safety_never_write_the_brake_row(tmp_path: Path) -> None:
    """The G7 scenario: a virgin ``agent_guardrails`` table AND no write grant.

    Both polls must answer 200 -- the masthead and the safety screen cannot go dark
    because a row was never seeded -- with the brake reading its honest default ('ok',
    mandate refusing on the first unset breaker) and NOT ONE write attempted. A poll
    that get-or-created would 503 here, on the two most-polled endpoints in the app.
    The denial is a class-level Engine listener because the app builds its OWN engine
    from the URL (``get_engine`` is not a cache), so it covers the app's writes too."""
    client, engine, _calls = _broker_app(tmp_path, FakeBroker())
    with _deny_writes("agent_guardrails") as attempted:
        gate = client.get("/api/gate")
        safety = client.get("/api/execution/safety")

    assert attempted == []                       # no INSERT/UPDATE even attempted
    assert gate.status_code == 200
    assert gate.json()["brake_state"] == "ok"    # the honest default, not a seeded row
    assert safety.status_code == 200
    body = safety.json()
    assert body["guardrails"] == {"ok": False, "reason": "max_daily_loss_usd is not set",
                                  "state": "ok", "sweep_state": None}
    # preflight rides this endpoint: its brake row is a peek too (no seed, no 503).
    checks = {c["name"]: c for c in body["preflight"]["checks"]}
    assert checks["guardrails"]["ok"] is False
    with Session(engine) as s:
        assert s.query(AgentGuardrails).count() == 0  # the table is still virgin


def test_execution_safety_broker_error_degrades_by_class_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RAISING factory degrades to the no-broker-shaped report with the
    reachable line carrying the exception CLASS only -- the message (which can
    embed hosts or credentials) never reaches the wire; still 200."""
    monkeypatch.setenv("SWING_BROKER", "alpaca")
    url = _db_url(tmp_path)
    get_engine(url)

    def exploding() -> FakeBroker | None:
        raise RuntimeError("secret-host.alpaca.markets credential nope")

    app = create_app(url, edge_dir=tmp_path,
                     latest_closes_fn=lambda tickers: {}, broker_factory=exploding)
    r = TestClient(app).get("/api/execution/safety")
    assert r.status_code == 200
    body = r.json()
    assert body["broker_configured"] is True  # settings truthiness, NOT connectivity
    checks = {c["name"]: c for c in body["preflight"]["checks"]}
    assert checks["reachable"]["ok"] is False
    assert checks["reachable"]["detail"] == "broker error (RuntimeError)"
    # The config line stays HONEST: SWING_BROKER IS set, the FACTORY failed --
    # it must not claim the variable is unset.
    assert "SWING_BROKER is unset" not in checks["config"]["detail"]
    assert body["preflight"]["go"] is False
    assert body["bracket_shield"]["known"] is False  # UNKNOWN, never green
    assert "secret-host" not in r.text  # leak posture: class name only


def test_execution_safety_unreachable_venue_leaks_class_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The safety screen's PRIMARY degraded scenario: the factory succeeds (a
    broker is configured) but ``get_account`` fails at the venue -- down, 401 --
    with a message embedding the venue host. The reachable line must carry the
    exception CLASS only; the message never reaches the wire."""
    monkeypatch.setenv("SWING_BROKER", "alpaca")

    class _DownVenueBroker(FakeBroker):
        def get_account(self) -> BrokerAccount:
            raise RuntimeError("secret-venue-host.alpaca.markets 401 unauthorized")

    client, _engine, _calls = _broker_app(tmp_path, _DownVenueBroker())
    r = client.get("/api/execution/safety")
    assert r.status_code == 200
    checks = {c["name"]: c for c in r.json()["preflight"]["checks"]}
    assert checks["reachable"]["ok"] is False
    assert "RuntimeError" in checks["reachable"]["detail"]  # class name on the line
    assert "secret-venue-host" not in r.text                # message never on the wire



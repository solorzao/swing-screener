"""Shared plumbing for the cockpit API test suite (split out of the old
test_api.py monolith): the app/client builders, seeded-row factories, the
read-only-grant listeners, and closed wire-shape sets used across the per-router
test files."""

import itertools
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from swing_screener.cockpit.api import create_app
from swing_screener.cockpit.common import (
    ActionNonce,
)
from swing_screener.db.models import (
    AnalysisRequest,
    AnalystCall,
    ExecutionLog,
    PaperTrade,
    Signal,
)
from swing_screener.db.session import get_engine
from swing_screener.pipeline.broker import BrokerOrderSpec, FakeBroker
from swing_screener.pipeline.proposed import (
    ProposedVariant,
    proposed_to_json,
    store_filename,
)
from swing_screener.pipeline.registry import Experiment

STAT_KEYS = {"value", "n", "n_clusters", "ci_low", "ci_high", "cost_level",
             "corpus_id", "facet", "unit", "thin_clusters"}


# --- read-only-grant simulation -------------------------------------------------------
#
# ONE implementation behind two names. Both refuse statements against the named
# TABLES with an ``OperationalError`` -- a real ``SQLAlchemyError``, which is what a
# denied write raises through the driver, so the endpoints' 503-never-200 posture is
# exercised end to end. (A bare ``PermissionError`` escapes SQLAlchemy's wrapping and
# 500s, which tests the harness rather than the app.)
#
# POSITIVE CONTROL, REQUIRED: a test whose assertion is "nothing was attempted"
# passes just as happily when the listener never bound at all. Every consumer of
# these helpers must ALSO prove the guard is live -- see
# ``test_deny_writes_listener_actually_fires`` beside the G7 test.

#: DML verbs only. The guards must NOT match sqlite's schema reflection
#: (``pragma main.table_info("agent_guardrails")``), which ``create_all`` runs while
#: the app builds its engine: denying that kills the app before any endpoint code
#: runs, and the test would "pass" against a request that never happened.
_DML_WRITES = ("insert into", "update", "delete from")
_DML_ALL = ("select", *_DML_WRITES)


@contextmanager
def _deny_sql(*tables: str, verbs: tuple[str, ...]) -> Iterator[list[str]]:
    """Refuse every ``verbs`` statement touching ``tables``; yield what was tried.

    Listens on the Engine CLASS, not one instance, because ``create_app`` builds its
    OWN engine from the URL -- an instance listener would miss exactly the statements
    under test."""
    attempted: list[str] = []

    def _guard(conn: Any, cursor: Any, statement: str, parameters: Any,
               context: Any, executemany: bool) -> None:
        sql = " ".join(statement.split()).lower()
        if sql.startswith(verbs) and any(t in sql for t in tables):
            attempted.append(sql)
            raise OperationalError(statement, {}, Exception("no grant"))

    event.listen(Engine, "before_cursor_execute", _guard)
    try:
        yield attempted
    finally:
        event.remove(Engine, "before_cursor_execute", _guard)


def _deny_writes(*tables: str) -> Any:
    """A read-only DB grant: INSERT/UPDATE/DELETE on ``tables`` raise, reads pass."""
    return _deny_sql(*tables, verbs=_DML_WRITES)


def _deny_reads(*tables: str) -> Any:
    """The complement: SELECTs on ``tables`` raise, writes pass. The shape of a
    failure that arrives AFTER a transition has committed -- the endpoint's write
    lands, only its follow-up read dies."""
    return _deny_sql(*tables, verbs=("select",))


def _deny_all(*tables: str) -> Any:
    """Harsher: reads and writes. For the emergency path, where DISARM must reach the
    venue whatever the database is doing."""
    return _deny_sql(*tables, verbs=_DML_ALL)


@pytest.fixture(autouse=True)
def _scrub_gh_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_app() reads SWING_GH_TOKEN/REPO at construction; on a dev shell
    exporting both, every app-building test wires REAL GitHub pollers (live API
    calls in tests) and the GH heartbeats read "up" instead of the asserted
    "unknown". Scrub directory-wide; poller tests re-set the vars via
    monkeypatch.setenv, which runs after this autouse fixture and wins."""
    monkeypatch.delenv("SWING_GH_TOKEN", raising=False)
    monkeypatch.delenv("SWING_GH_REPO", raising=False)

def _trade(ticker: str, r: float, *, play_type: str = "reversal",
           strength: str | None = "confirmed", would_surface: bool | None = None,
           exit_date: date | None = None) -> PaperTrade:
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account="research", play_type=play_type, strength=strength,
        would_surface=would_surface, fill_status="filled", stop=95.0, target=110.0,
        risk=5.0, status="closed", realized_r=r, exit_date=exit_date,
    )


def _book_trade(ticker: str, r: float, *, arm: str = "baseline", variant: str = "default",
                trigger_ts: datetime | None = None, exit_date: date | None = None,
                would_surface: bool | None = None) -> PaperTrade:
    """A closed research-grid row for the forward-books loader: arm/variant/trigger_ts
    are the loader's filter keys and (for arms) the pair identity."""
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account="research", play_type="reversal", strength="confirmed", arm=arm,
        variant=variant, trigger_ts=trigger_ts, would_surface=would_surface,
        fill_status="filled", stop=95.0, target=110.0, risk=5.0, status="closed",
        realized_r=r, exit_date=exit_date,
    )


def _experiment(name: str, *, kind: str, status: str = "active",
                decision: str | None = None) -> Experiment:
    return Experiment(
        name=name, kind=kind, play_type="reversal",
        control="baseline" if kind == "arm" else "default",
        hypothesis="h", stopping_rule="rule text verbatim", mde_r=0.10,
        target_ci_halfwidth_r=0.15, registered_at="2026-07-10",
        registered_sha="abc123", doc_ref="docs/x.md", provenance="test",
        status=status, decided_at="2026-07-01" if decision else None, decision=decision,
    )


def _write_registry(edge_dir: Path, experiments: list[Experiment]) -> None:
    payload = "[" + ",".join(e.as_json() for e in experiments) + "]"
    (edge_dir / "experiments.json").write_text(payload, encoding="utf-8")


def _db_url(tmp_path: Path) -> str:
    # as_posix(): backslashes in a sqlite URL are asking for escape trouble.
    return f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"


def _client(tmp_path: Path) -> TestClient:
    url = _db_url(tmp_path)
    get_engine(url)  # seed the file + schema; the app builds its OWN engine from the URL
    return TestClient(create_app(url, edge_dir=tmp_path))


_HDR = {"X-Cockpit": "1"}
_AZURE_URL = "mssql+pyodbc://@srv.database.windows.net/swing?driver=ODBC+Driver+18"


def _call(created: date, cost: float | None) -> AnalystCall:
    return AnalystCall(
        created_date=created, ticker="AMD", timeframe="1d", play_type="continuation",
        run_date=created, baseline_conviction="medium", final_conviction="high",
        nudge_reason="test", model="opus", est_cost_usd=cost,
    )


def _nonce_of(app: Any) -> ActionNonce:
    """The app's post-action nonce (parked on app.state by create_app)."""
    nonce = app.state.action_nonce
    assert isinstance(nonce, ActionNonce)
    return nonce


def _trade_body(**over: object) -> dict[str, object]:
    """A valid log-trade body; override any field to break one rule at a time."""
    body: dict[str, object] = {"ticker": " amd ", "entry_price": 100.0, "size": 10.0,
                               "stop": 95.0, "target": 110.0}
    body.update(over)
    return body


def _signal_row(**over: object) -> Signal:
    """A prefill-source Signal: zone [96, 101], stop 95 (zone-R risk = 6), target 110."""
    row: dict[str, object] = {
        "run_date": date(2026, 7, 10), "ticker": "AMD", "timeframe": "1d", "horizon": "medium",
        "score": 0.9, "rank": 1, "trigger_close": 100.0, "atr": 4.0, "rsi": 55.0,
        "entry_floor": 96.0, "entry_ceiling": 101.0, "stop": 95.0, "target": 110.0,
    }
    row.update(over)
    return Signal(**row)


def _client_and_engine(tmp_path: Path) -> tuple[TestClient, Engine]:
    url = _db_url(tmp_path)
    engine = get_engine(url)  # seeds the file + schema; the app builds its own engine
    return TestClient(create_app(url, edge_dir=tmp_path)), engine


_IDEM = itertools.count()


def _positions_client(
    tmp_path: Path, prices: dict[str, float] | None = None,
    broker: FakeBroker | None = None,
) -> tuple[TestClient, Engine]:
    """An app wired through BOTH livedata seams: quotes from a fixed dict (misses
    absent, mirroring latest_closes), broker from a FakeBroker or None -- the suite
    never touches yfinance or a venue."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    fixed = dict(prices or {})
    app = create_app(url, edge_dir=tmp_path,
                     latest_closes_fn=lambda tickers: fixed,
                     broker_factory=lambda: broker)
    return TestClient(app), engine


def _exec_log(**over: object) -> ExecutionLog:
    """One ExecutionLog row; idempotency_key auto-uniqued so seeds never collide."""
    row: dict[str, object] = {
        "created_date": date(2026, 7, 8), "ticker": "AMD", "timeframe": "1d",
        "play_type": "continuation", "run_date": date(2026, 7, 8), "account": "manual",
        "mode": "manual", "side": "buy", "limit_price": 100.0, "shares": 10, "stop": 95.0,
        "target": 110.0, "risk_dollars": 50.0, "notional": 1000.0, "status": "recorded",
        "detail": "seeded", "idempotency_key": f"seed-{next(_IDEM)}",
    }
    row.update(over)
    return ExecutionLog(**row)


def _analysis_row(**over: object) -> AnalysisRequest:
    """One queue row; datetimes are NAIVE, exactly as sqlite/mssql hand them back."""
    row: dict[str, object] = {
        "ticker": "AMD", "requested_at": datetime(2026, 7, 10, 12, 0),  # noqa: DTZ001 -- naive, mirrors sqlite/mssql
        "status": "queued"}
    row.update(over)
    return AnalysisRequest(**row)


def _proposal(name: str, play_type: str,
              delta: dict[str, float | int | str | bool],
              *, status: str = "queued") -> ProposedVariant:
    return ProposedVariant(
        name=name, play_type=play_type, delta=delta, rationale="hunch text",
        hunch_ref="reversal:2026-07-01:3", status=status,
        drafted_at="2026-07-10", provenance="analyst:test",
    )


def _write_proposals(edge_dir: Path, play_type: str,
                     items: list[ProposedVariant]) -> None:
    (edge_dir / store_filename(play_type)).write_text(
        proposed_to_json(items), encoding="utf-8")


def _disarm_broker(broker: FakeBroker | None = None) -> FakeBroker:
    """The disarm scenario at the venue: a resting AMD entry limit (fake-0) plus a
    bracket-filled NVDA position whose protective sell legs (stop + target) are
    LIVE open orders. Pass a FakeBroker SUBCLASS to script a mid-disarm venue
    failure over the same scenario."""
    broker = broker if broker is not None else FakeBroker()
    broker.submit_order(BrokerOrderSpec(
        client_order_id="resting-entry", symbol="AMD", side="buy", qty=3,
        order_type="limit", limit_price=90.0, time_in_force="day"))
    entry = broker.submit_order(BrokerOrderSpec(
        client_order_id="bracket-entry", symbol="NVDA", side="buy", qty=8,
        order_type="limit", limit_price=100.0, time_in_force="day",
        stop_loss=95.0, take_profit=110.0))
    broker.fill(entry.broker_order_id, 100.0)
    return broker


def _kill_sell_legs(broker: FakeBroker) -> None:
    """Strip the venue-held sell legs (the pre-fix blanket-disarm damage)."""
    for order in list(broker.list_open_orders()):
        if order.side == "sell":
            broker.cancel_order(order.broker_order_id)


def _broker_app(
    tmp_path: Path, broker: FakeBroker | None,
) -> tuple[TestClient, Engine, dict[str, int]]:
    """(client, engine, factory-call counter) wired through the broker seam; the
    counter observes snapshot refreshes + the disarm endpoint's live resolution."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    calls = {"n": 0}

    def factory() -> FakeBroker | None:
        calls["n"] += 1
        return broker

    app = create_app(url, edge_dir=tmp_path,
                     latest_closes_fn=lambda tickers: {}, broker_factory=factory)
    return TestClient(app), engine, calls


def _recorded_stop_row(ticker: str, stop: float) -> ExecutionLog:
    """A live ticket whose recorded stop latest_recorded_stop reads back."""
    return _exec_log(ticker=ticker, account="live", mode="live", stop=stop,
                     shares=8, status="submitted_live")


_RUN_D = date(2026, 7, 10)
def _grade_call(ticker: str, *, play_type: str = "reversal", final: str = "high",
                run_date: date = _RUN_D, realized_r: float | None = None,
                scored_at: date | None = None) -> AnalystCall:
    return AnalystCall(
        created_date=run_date, ticker=ticker, timeframe="1d", play_type=play_type,
        run_date=run_date, baseline_conviction="medium", final_conviction=final,
        nudge_reason="test nudge", model="test-model",
        realized_r=realized_r, scored_at=scored_at,
    )



"""DISARM's DisarmEvent audit trail on the FAILURE path (routers/safety.py).

The success path ("a real disarm records reason 'cockpit'") is pinned in
test_api.py; this file pins the gap that motivated the fix: a disarm that cancels
the entry orders and then dies HAS moved venue state, and the System Behavior
Auditor's breach scan exists to flag exactly such disarms -- so the failure path
must persist a DisarmEvent too (reason 'cockpit-partial'), best-effort, never
masking the original 503. Helpers are deliberate local copies of test_api.py's
disarm scenario (same FakeBroker seeding), so this file stands alone.

The cancel-loop failure is still that raising 503 path. A refused STOP RE-SUBMIT
no longer is: since ``ensure_stop_protection`` grew a per-position boundary
(2026-07-25) it triages the refusal into ``unprotected`` -- the alarm that names
the position -- and sweeps the rest of the book instead of aborting on the first
one. Both postures are pinned below.
"""

from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import create_app
from swing_screener.db.models import DisarmEvent, ExecutionLog
from swing_screener.db.session import get_engine
from swing_screener.pipeline.broker import BrokerOrderSpec, FakeBroker

_HDR = {"X-Cockpit": "1"}


def _disarm_broker(broker: FakeBroker) -> FakeBroker:
    """The disarm scenario at the venue (mirrors test_api.py): a resting AMD entry
    limit plus a bracket-filled NVDA position whose protective sell legs (stop +
    target) are LIVE open orders."""
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
    """Strip the venue-held sell legs so the stop-restore path has work to do."""
    for order in list(broker.list_open_orders()):
        if order.side == "sell":
            broker.cancel_order(order.broker_order_id)


def _broker_client(tmp_path: Path, broker: FakeBroker) -> tuple[TestClient, Any]:
    url = f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"
    engine = get_engine(url)
    app = create_app(url, edge_dir=tmp_path,
                     latest_closes_fn=lambda tickers: {}, broker_factory=lambda: broker)
    return TestClient(app), engine


def _recorded_stop_row(ticker: str, stop: float) -> ExecutionLog:
    """A LIVE ticket whose recorded stop latest_recorded_stop reads back."""
    return ExecutionLog(
        created_date=date(2026, 7, 8), ticker=ticker, timeframe="1d",
        play_type="continuation", run_date=date(2026, 7, 8), account="live",
        mode="live", side="buy", limit_price=100.0, shares=8, stop=stop,
        target=110.0, risk_dollars=50.0, notional=1000.0, status="submitted_live",
        detail="seeded", idempotency_key=f"seed-{ticker}-{stop}",
    )


def test_disarm_reports_a_refused_stop_as_unprotected_and_keeps_going(
    tmp_path: Path,
) -> None:
    """A venue that REFUSES one stop re-submit no longer aborts the sweep.

    ``ensure_stop_protection``'s per-position boundary (2026-07-25) triages the
    failure instead of raising: the refused symbol joins ``unprotected`` -- the same
    red no-Escape alarm the no-recorded-level case raises, now NAMING the position --
    while every other position is still swept. That is strictly safer than the old
    posture, where the first refusal raised a 503 that named nothing and left the
    REMAINING positions unattempted.

    The conduct record still says PARTIAL: a completed sweep that left a position
    unprotected is partially-protected venue state, so the DisarmEvent reads
    'cockpit-partial' -- the more-alarming context the Auditor's disarm narrative
    exists to surface -- even though nothing raised. Leak posture unchanged: no
    venue host on the wire."""

    class _RestoreRefusedBroker(FakeBroker):
        def submit_order(self, spec: BrokerOrderSpec) -> Any:
            if spec.client_order_id.startswith("disarm-stop-NVDA"):
                raise RuntimeError("secret-venue-host refused the stop re-submit")
            return super().submit_order(spec)

    broker = _disarm_broker(_RestoreRefusedBroker())
    # a SECOND naked position, to prove the pass continues past the refusal
    other = broker.submit_order(BrokerOrderSpec(
        client_order_id="entry-TSLA", symbol="TSLA", side="buy", qty=4,
        order_type="limit", limit_price=200.0, time_in_force="day"))
    broker.fill(other.broker_order_id, 200.0)
    _kill_sell_legs(broker)  # NVDA's stop leg is dead -> the restore path runs
    client, engine = _broker_client(tmp_path, broker)
    with Session(engine) as s:
        s.add(_recorded_stop_row("NVDA", 95.0))
        s.add(_recorded_stop_row("TSLA", 190.0))
        s.commit()

    r = client.post("/api/disarm", headers=_HDR)

    assert r.status_code == 200
    body = r.json()
    assert body["unprotected"] == ["NVDA"]            # the alarm NAMES the position
    assert body["stops_restored"] == ["TSLA"]         # ...and the rest was protected
    assert "secret-venue-host" not in r.text          # leak posture holds
    with Session(engine) as s:
        ev = s.query(DisarmEvent).one()               # the Auditor SEES the sweep
        assert ev.reason == "cockpit-partial"         # ...and that it left a gap
        assert ev.orders_cancelled == 1               # AMD's resting entry was pulled


def test_disarm_failure_during_the_cancel_loop_records_the_attempt(
    tmp_path: Path,
) -> None:
    """A raise DURING the cancel loop leaves the pulled-entries count unknowable, so
    the recorded event reports the honest floor: 0 cancelled, reason
    'cockpit-partial'. The attempt itself is still visible to the Auditor -- from
    here we cannot prove no cancel landed, so silence would be the lie."""

    class _CancelRefusedBroker(FakeBroker):
        def cancel_order(self, broker_order_id: str) -> None:
            raise RuntimeError("venue refused the cancel")

    broker = _disarm_broker(_CancelRefusedBroker())
    client, engine = _broker_client(tmp_path, broker)
    assert client.post("/api/disarm", headers=_HDR).status_code == 503
    with Session(engine) as s:
        ev = s.query(DisarmEvent).one()
        assert ev.reason == "cockpit-partial"
        assert ev.orders_cancelled == 0


def test_disarm_dry_run_failure_records_no_event(tmp_path: Path) -> None:
    """A FAILED dry run records nothing: it never touched the venue, so there is no
    disarm for the Auditor to see -- the failure-path record is gated on the same
    not-dry_run as the snapshot invalidation and the wake."""

    class _ListRefusedBroker(FakeBroker):
        def list_open_orders(self) -> Any:
            raise RuntimeError("venue down")

    broker = _disarm_broker(_ListRefusedBroker())
    client, engine = _broker_client(tmp_path, broker)
    assert client.post("/api/disarm?dry_run=1", headers=_HDR).status_code == 503
    with Session(engine) as s:
        assert s.query(DisarmEvent).count() == 0


def test_disarm_completed_run_still_records_reason_cockpit(tmp_path: Path) -> None:
    """The success path through the UNIFIED recording site: a completed real run
    still writes reason 'cockpit' with the true cancel count -- the refactor that
    added the failure-path event must not have renamed the success reason (the
    Auditor's disarm scan keys on the row existing; humans read the reason)."""
    broker = _disarm_broker(FakeBroker())
    client, engine = _broker_client(tmp_path, broker)
    assert client.post("/api/disarm", headers=_HDR).status_code == 200
    with Session(engine) as s:
        ev = s.query(DisarmEvent).one()
        assert ev.reason == "cockpit"
        assert ev.orders_cancelled == 1

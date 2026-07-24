"""The disarm command: pull entry-side orders; keep positions stop-protected.

Flipping the mode off stops NEW submits but never touched what was already at the
venue -- resting entry limits could fill AFTER a disarm. `python -m
swing_screener.pipeline.disarm` closes that gap; positions stay (a human decision).

Since brackets (PR #92) the venue also holds each position's protective STOP leg, so
disarm must cancel ONLY entry-side buys -- the old blanket cancel stripped the stops
off the very positions it refuses to close, handing the operator an unprotected book
at the moment of maximum stress. When a stop leg is already dead, disarm re-submits a
plain stop at the ExecutionLog ticket's RECORDED level (copied, never computed).
"""

import logging
from datetime import date

import pytest
from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.session import get_engine
from swing_screener.pipeline import disarm
from swing_screener.pipeline.broker import BrokerOrderSpec, FakeBroker


def _spec(key: str, symbol: str, **overrides: object) -> BrokerOrderSpec:
    base: dict[str, object] = {"client_order_id": key, "symbol": symbol, "side": "buy",
                                   "qty": 10, "order_type": "limit", "limit_price": 100.0,
                                   "time_in_force": "day"}
    base.update(overrides)
    return BrokerOrderSpec(**base)  # type: ignore[arg-type]


def _armed_broker() -> FakeBroker:
    broker = FakeBroker(real_money=False)
    broker.submit_order(_spec("k1", "AMD"))            # resting entry limit
    filled = broker.submit_order(_spec("k2", "NVDA"))  # a filled position (no bracket)
    broker.fill(filled.broker_order_id, 100.0)
    return broker


def _armed_bracket_broker() -> FakeBroker:
    """A resting AMD entry limit + a bracket-filled NVDA position whose venue-held
    sell legs (protective stop + target) are LIVE open orders."""
    broker = FakeBroker(real_money=False)
    broker.submit_order(_spec("k1", "AMD"))
    entry = broker.submit_order(_spec("k2", "NVDA", qty=8, stop_loss=95.0,
                                      take_profit=110.0))
    broker.fill(entry.broker_order_id, 100.0)
    return broker


def _kill_legs(broker: FakeBroker) -> None:
    """Simulate the OLD blanket disarm having already stripped the sell legs."""
    for order in list(broker.list_open_orders()):
        if order.side == "sell":
            broker.cancel_order(order.broker_order_id)


def _log_fields(*, ticker: str = "NVDA", stop: float = 95.0,
                **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "created_date": date(2026, 7, 3), "ticker": ticker, "timeframe": "1d",
        "play_type": "continuation", "run_date": date(2026, 7, 3), "account": "live",
        "mode": "alpaca", "side": "buy", "limit_price": 100.0, "shares": 8, "stop": stop,
        "target": 110.0, "risk_dollars": 40.0, "notional": 800.0, "status": "submitted_live",
        "detail": "live order",
        "idempotency_key": f"{ticker}|{stop}|test"}
    base.update(overrides)
    return base


def _db(tmp_path, monkeypatch, *rows: dict[str, object]) -> str:
    """A tmp sqlite the disarm CLI resolves via SWING_DB_URL, seeded with tickets."""
    url = f"sqlite:///{tmp_path / 'disarm.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    with Session(get_engine(url)) as s:
        for fields in rows:
            repo.add_execution_log(s, **fields)
        s.commit()
    return url


def test_disarm_cancels_resting_orders_but_never_positions(tmp_path, monkeypatch):
    broker = _armed_broker()
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(disarm, "build_broker", lambda settings: broker)
    monkeypatch.setattr("sys.argv", ["disarm"])

    disarm.main()

    assert broker.list_open_orders() == []          # every resting ENTRY pulled
    assert len(broker.get_positions()) == 1         # the position is NOT auto-closed


def test_disarm_cancels_entries_but_keeps_protective_stops(tmp_path, monkeypatch):
    """THE bug (2026-07-04 review): a blanket cancel-all stripped the bracket's
    protective sell legs off the position disarm deliberately leaves open."""
    broker = _armed_bracket_broker()
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(disarm, "build_broker", lambda settings: broker)
    monkeypatch.setattr("sys.argv", ["disarm"])

    disarm.main()

    open_orders = broker.list_open_orders()
    assert [o for o in open_orders if o.side == "buy"] == []   # entries pulled
    stops = [o for o in open_orders if o.side == "sell" and o.order_type == "stop"]
    targets = [o for o in open_orders if o.side == "sell" and o.order_type == "limit"]
    assert [o.symbol for o in stops] == ["NVDA"]     # the protective stop SURVIVES
    assert [o.symbol for o in targets] == ["NVDA"]   # the target leg survives too
    assert len(broker.get_positions()) == 1


def test_disarm_restores_a_dead_stop_leg_at_the_recorded_level(tmp_path, monkeypatch):
    """A position whose stop leg already died (e.g. a pre-fix blanket disarm) gets a
    plain protective stop re-submitted at the NEWEST live ticket's recorded level."""
    broker = _armed_bracket_broker()
    _kill_legs(broker)
    _db(tmp_path, monkeypatch,
        _log_fields(stop=90.0),                             # older ticket
        _log_fields(stop=95.0, status="filled_live"))       # newest live ticket wins
    monkeypatch.setattr(disarm, "build_broker", lambda settings: broker)
    monkeypatch.setattr("sys.argv", ["disarm"])

    disarm.main()

    stops = [o for o in broker.list_open_orders()
             if o.side == "sell" and o.order_type == "stop"]
    assert [o.symbol for o in stops] == ["NVDA"]
    restored = broker.submitted_specs[-1]
    assert restored.side == "sell"
    assert restored.order_type == "stop"
    assert restored.stop_price == 95.0        # COPIED from the ticket, never computed
    assert restored.qty == 8                  # covers the whole position
    assert restored.time_in_force == "gtc"    # protection must not expire at EOD


def test_disarm_reports_unprotected_when_no_recorded_stop(tmp_path, monkeypatch, caplog):
    """No live stop at the venue and no ticket to copy a level from -> disarm refuses
    to guess a level (North Star #4) and reports the position LOUDLY."""
    broker = _armed_bracket_broker()
    _kill_legs(broker)
    _db(tmp_path, monkeypatch)  # no ExecutionLog rows at all
    monkeypatch.setattr(disarm, "build_broker", lambda settings: broker)
    monkeypatch.setattr("sys.argv", ["disarm"])

    with caplog.at_level(logging.ERROR):
        disarm.main()

    sells = [o for o in broker.list_open_orders() if o.side == "sell"]
    assert sells == []                                   # nothing invented
    assert any("UNPROTECTED" in r.message for r in caplog.records)


def test_ensure_stop_protection_returns_restored_and_unprotected() -> None:
    """The (restored, unprotected) contract: ``restored`` names every symbol a
    protective stop was re-submitted for, ``unprotected`` every position with no
    recorded level (left alone, loudly) -- the cockpit's DISARM response is built
    from exactly this pair."""
    broker = FakeBroker(real_money=False)
    for symbol in ("NVDA", "XYZY"):
        entry = broker.submit_order(_spec(f"k-{symbol}", symbol, qty=8,
                                          stop_loss=95.0, take_profit=110.0))
        broker.fill(entry.broker_order_id, 100.0)
    _kill_legs(broker)
    stops = {"NVDA": 95.0}  # XYZY has no recorded level

    restored, unprotected = disarm.ensure_stop_protection(
        broker, stops.get, key_suffix="test")

    assert restored == ["NVDA"]
    assert unprotected == ["XYZY"]
    live_stops = [o for o in broker.list_open_orders()
                  if o.side == "sell" and o.order_type == "stop"]
    assert [o.symbol for o in live_stops] == ["NVDA"]  # restored means SUBMITTED


def test_ensure_stop_protection_dry_run_reports_restored_without_submitting() -> None:
    """Dry-run still POPULATES ``restored`` (the hold preview must show what a real
    run would protect) while the venue stays untouched."""
    broker = _armed_bracket_broker()
    _kill_legs(broker)
    n_specs = len(broker.submitted_specs)
    stops = {"NVDA": 95.0}

    restored, unprotected = disarm.ensure_stop_protection(
        broker, stops.get, key_suffix="test", dry_run=True)

    assert restored == ["NVDA"]                    # the preview names the symbol...
    assert unprotected == []
    assert len(broker.submitted_specs) == n_specs  # ...but nothing was submitted


def test_disarm_dry_run_touches_nothing(tmp_path, monkeypatch):
    broker = _armed_bracket_broker()
    _kill_legs(broker)
    _db(tmp_path, monkeypatch, _log_fields(stop=95.0))
    n_specs = len(broker.submitted_specs)
    monkeypatch.setattr(disarm, "build_broker", lambda settings: broker)
    monkeypatch.setattr("sys.argv", ["disarm", "--dry-run"])

    disarm.main()

    assert len([o for o in broker.list_open_orders() if o.side == "buy"]) == 1
    assert len(broker.submitted_specs) == n_specs        # no restore submitted
    assert len(broker.get_positions()) == 1


def test_disarm_without_a_broker_exits_loudly(monkeypatch):
    monkeypatch.setattr(disarm, "build_broker", lambda settings: None)
    monkeypatch.setattr("sys.argv", ["disarm"])

    with pytest.raises(SystemExit, match="no broker configured"):
        disarm.main()

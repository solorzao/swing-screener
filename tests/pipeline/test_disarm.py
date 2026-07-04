"""The disarm command: pull every resting order; never auto-close positions.

Flipping the mode off stops NEW submits but never touched what was already at the
venue -- resting entry limits could fill AFTER a disarm. `python -m
swing_screener.pipeline.disarm` closes that gap; positions stay (a human decision).
"""

import pytest

from swing_screener.pipeline import disarm
from swing_screener.pipeline.broker import BrokerOrderSpec, FakeBroker


def _spec(key: str, symbol: str) -> BrokerOrderSpec:
    return BrokerOrderSpec(client_order_id=key, symbol=symbol, side="buy", qty=10,
                           order_type="limit", limit_price=100.0, time_in_force="day")


def _armed_broker() -> FakeBroker:
    broker = FakeBroker(real_money=False)
    broker.submit_order(_spec("k1", "AMD"))            # resting entry limit
    filled = broker.submit_order(_spec("k2", "NVDA"))  # a filled position
    broker.fill(filled.broker_order_id, 100.0)
    return broker


def test_disarm_cancels_resting_orders_but_never_positions(monkeypatch):
    broker = _armed_broker()
    monkeypatch.setattr(disarm, "build_broker", lambda settings: broker)
    monkeypatch.setattr("sys.argv", ["disarm"])

    disarm.main()

    assert broker.list_open_orders() == []          # every resting order pulled
    assert len(broker.get_positions()) == 1         # the position is NOT auto-closed


def test_disarm_dry_run_touches_nothing(monkeypatch):
    broker = _armed_broker()
    monkeypatch.setattr(disarm, "build_broker", lambda settings: broker)
    monkeypatch.setattr("sys.argv", ["disarm", "--dry-run"])

    disarm.main()

    assert len(broker.list_open_orders()) == 1      # still resting
    assert len(broker.get_positions()) == 1


def test_disarm_without_a_broker_exits_loudly(monkeypatch):
    monkeypatch.setattr(disarm, "build_broker", lambda settings: None)
    monkeypatch.setattr("sys.argv", ["disarm"])

    with pytest.raises(SystemExit, match="no broker configured"):
        disarm.main()

"""Tests for the PURE, deterministic broker seam (``pipeline.broker``).

No network, no DB, no datetime, no random: ``FakeBroker`` is an in-memory test double
that implements the ``BrokerClient`` Protocol. The load-bearing properties later tasks
(the LiveAdapter + the reconciler) lean on are asserted here:

* idempotency on ``client_order_id`` -- a re-submit of the same client order id returns
  the SAME order, never opening a second one (mirrors the execution adapters' idempotency
  key) -- so a retried submit can never double up.
* deterministic ``broker_order_id``s (``fake-0``, ``fake-1``, ...) -- no random, no clock,
  so a test can assert exact ids.
* the scripting helpers (``fill`` / ``partially_fill`` / ``reject`` / ``close_position``)
  let Tasks 4/5/7 drive the broker through a lifecycle without a real venue.
"""

from swing_screener.pipeline.broker import (
    BrokerAccount,
    BrokerClient,
    BrokerOrder,
    BrokerOrderSpec,
    BrokerPosition,
    FakeBroker,
)


def _spec(*, client_order_id: str = "k1", symbol: str = "AAPL", qty: int = 10,
          limit_price: float | None = 100.0) -> BrokerOrderSpec:
    return BrokerOrderSpec(
        client_order_id=client_order_id,
        symbol=symbol,
        side="buy",
        qty=qty,
        order_type="limit",
        limit_price=limit_price,
        time_in_force="day",
    )


# ---------------------------------------------------------------------------
# submit -> get_order round-trip + the idempotency property.
# ---------------------------------------------------------------------------
def test_submit_then_get_order_round_trips_as_new() -> None:
    broker = FakeBroker()
    order = broker.submit_order(_spec(symbol="MSFT", qty=5))

    assert isinstance(order, BrokerOrder)
    assert order.status == "new"
    assert order.filled_qty == 0
    assert order.filled_avg_price is None
    assert order.symbol == "MSFT"
    assert order.client_order_id == "k1"

    # get_order hands the same order back by broker_order_id.
    assert broker.get_order(order.broker_order_id) == order


def test_submit_is_idempotent_on_client_order_id() -> None:
    broker = FakeBroker()
    first = broker.submit_order(_spec(client_order_id="dup"))
    second = broker.submit_order(_spec(client_order_id="dup", qty=999))  # same key

    # SAME order handed back (the second submit is a no-op), and only one exists.
    assert second == first
    assert second.broker_order_id == first.broker_order_id
    assert len(broker.list_open_orders()) == 1


# ---------------------------------------------------------------------------
# deterministic broker_order_ids.
# ---------------------------------------------------------------------------
def test_broker_order_ids_are_deterministic() -> None:
    broker = FakeBroker()
    a = broker.submit_order(_spec(client_order_id="a"))
    b = broker.submit_order(_spec(client_order_id="b"))
    assert a.broker_order_id == "fake-0"
    assert b.broker_order_id == "fake-1"


# ---------------------------------------------------------------------------
# fill -> filled status + a position; excluded from open orders.
# ---------------------------------------------------------------------------
def test_fill_sets_filled_status_and_opens_a_position() -> None:
    broker = FakeBroker()
    order = broker.submit_order(_spec(symbol="NVDA", qty=8))

    broker.fill(order.broker_order_id, price=120.5)

    filled = broker.get_order(order.broker_order_id)
    assert filled.status == "filled"
    assert filled.filled_qty == 8  # defaults to the order qty
    assert filled.filled_avg_price == 120.5

    # a position is now visible, and the order is no longer open.
    positions = broker.get_positions()
    assert positions == [BrokerPosition(symbol="NVDA", qty=8, avg_entry_price=120.5)]
    assert broker.list_open_orders() == []


def test_fill_with_explicit_qty() -> None:
    broker = FakeBroker()
    order = broker.submit_order(_spec(symbol="TSLA", qty=10))
    broker.fill(order.broker_order_id, price=200.0, qty=10)
    assert broker.get_positions() == [
        BrokerPosition(symbol="TSLA", qty=10, avg_entry_price=200.0)
    ]


# ---------------------------------------------------------------------------
# partially_fill -> still open.
# ---------------------------------------------------------------------------
def test_partially_fill_stays_open() -> None:
    broker = FakeBroker()
    order = broker.submit_order(_spec(symbol="AMD", qty=10))

    broker.partially_fill(order.broker_order_id, price=90.0, qty=4)

    partial = broker.get_order(order.broker_order_id)
    assert partial.status == "partially_filled"
    assert partial.filled_qty == 4
    assert partial.filled_avg_price == 90.0
    # still an open order.
    assert broker.list_open_orders() == [partial]


# ---------------------------------------------------------------------------
# reject -> not open.
# ---------------------------------------------------------------------------
def test_reject_marks_rejected_and_not_open() -> None:
    broker = FakeBroker()
    order = broker.submit_order(_spec())

    broker.reject(order.broker_order_id)

    rejected = broker.get_order(order.broker_order_id)
    assert rejected.status == "rejected"
    assert broker.list_open_orders() == []


# ---------------------------------------------------------------------------
# cancel_order / cancel_all_orders -> canceled, not open.
# ---------------------------------------------------------------------------
def test_cancel_order_marks_canceled_and_not_open() -> None:
    broker = FakeBroker()
    order = broker.submit_order(_spec())

    broker.cancel_order(order.broker_order_id)

    canceled = broker.get_order(order.broker_order_id)
    assert canceled.status == "canceled"
    assert broker.list_open_orders() == []


def test_cancel_all_orders_cancels_every_open_order() -> None:
    broker = FakeBroker()
    a = broker.submit_order(_spec(client_order_id="a", symbol="AAA"))
    b = broker.submit_order(_spec(client_order_id="b", symbol="BBB"))
    # one already filled -> cancel_all leaves it filled, only opens get canceled.
    broker.fill(a.broker_order_id, price=10.0)

    broker.cancel_all_orders()

    assert broker.get_order(a.broker_order_id).status == "filled"
    assert broker.get_order(b.broker_order_id).status == "canceled"
    assert broker.list_open_orders() == []


# ---------------------------------------------------------------------------
# close_position -> the position disappears (simulating a venue exit).
# ---------------------------------------------------------------------------
def test_close_position_removes_the_position() -> None:
    broker = FakeBroker()
    order = broker.submit_order(_spec(symbol="GOOG", qty=3))
    broker.fill(order.broker_order_id, price=50.0)
    assert broker.get_positions() == [
        BrokerPosition(symbol="GOOG", qty=3, avg_entry_price=50.0)
    ]

    broker.close_position("GOOG")

    assert broker.get_positions() == []


# ---------------------------------------------------------------------------
# close_position scripts the venue's close price -> last_close_price reads it back.
# The reconciler sources a live exit's exit_price from this (the broker owns the price).
# ---------------------------------------------------------------------------
def test_close_position_scripts_the_exit_price() -> None:
    broker = FakeBroker()
    order = broker.submit_order(_spec(symbol="GOOG", qty=3))
    broker.fill(order.broker_order_id, price=50.0)

    broker.close_position("GOOG", price=57.5)

    assert broker.get_positions() == []
    assert broker.last_close_price("GOOG") == 57.5


def test_last_close_price_none_when_never_closed() -> None:
    broker = FakeBroker()
    order = broker.submit_order(_spec(symbol="GOOG", qty=3))
    broker.fill(order.broker_order_id, price=50.0)
    # an open position has no scripted close yet.
    assert broker.last_close_price("GOOG") is None


# ---------------------------------------------------------------------------
# is_real_money honors the flag (default False).
# ---------------------------------------------------------------------------
def test_is_real_money_defaults_false() -> None:
    assert FakeBroker().is_real_money() is False
    assert FakeBroker().name == "fake"


def test_is_real_money_when_flagged() -> None:
    assert FakeBroker(real_money=True).is_real_money() is True


# ---------------------------------------------------------------------------
# get_account: defaults to a funded, ACTIVE account; the knobs override it.
# ---------------------------------------------------------------------------
def test_get_account_defaults_funded_and_active() -> None:
    account = FakeBroker().get_account()
    assert isinstance(account, BrokerAccount)
    assert account.status == "ACTIVE"
    assert account.cash == 100_000.0
    assert account.buying_power == 100_000.0


def test_get_account_honors_the_constructor_knobs() -> None:
    account = FakeBroker(cash=250.0, buying_power=0.0, status="HALTED").get_account()
    assert account.cash == 250.0
    assert account.buying_power == 0.0
    assert account.status == "HALTED"


# ---------------------------------------------------------------------------
# Bracket-leg modeling: a filled bracket entry leaves LIVE venue-held sell legs
# (the protective stop + the target), exactly like Alpaca does -- so tests can
# prove a disarm/kill-switch keeps positions stop-protected.
# ---------------------------------------------------------------------------
def _bracket_spec(*, client_order_id: str = "b1", symbol: str = "NVDA",
                  qty: int = 8) -> BrokerOrderSpec:
    return BrokerOrderSpec(
        client_order_id=client_order_id, symbol=symbol, side="buy", qty=qty,
        order_type="limit", limit_price=100.0, time_in_force="day",
        stop_loss=95.0, take_profit=110.0)


def test_orders_carry_side_and_order_type() -> None:
    order = FakeBroker().submit_order(_spec(symbol="MSFT"))
    assert order.side == "buy"
    assert order.order_type == "limit"


def test_fill_of_bracket_entry_spawns_live_protective_legs() -> None:
    broker = FakeBroker()
    entry = broker.submit_order(_bracket_spec(symbol="NVDA", qty=8))

    broker.fill(entry.broker_order_id, price=100.0)

    open_orders = broker.list_open_orders()
    stops = [o for o in open_orders if o.side == "sell" and o.order_type == "stop"]
    targets = [o for o in open_orders if o.side == "sell" and o.order_type == "limit"]
    assert [o.symbol for o in stops] == ["NVDA"]   # the venue-held protective stop
    assert [o.symbol for o in targets] == ["NVDA"]  # the venue-held target
    assert stops[0].filled_qty == 0 and stops[0].status == "new"
    # the position itself opened as usual.
    assert broker.get_positions() == [
        BrokerPosition(symbol="NVDA", qty=8, avg_entry_price=100.0)
    ]


def test_cancel_all_orders_kills_bracket_stop_legs_like_the_venue() -> None:
    # Documents the REAL venue semantic that makes a blanket cancel dangerous:
    # DELETE /v2/orders pulls the protective legs of a FILLED position too.
    broker = FakeBroker()
    entry = broker.submit_order(_bracket_spec())
    broker.fill(entry.broker_order_id, price=100.0)

    broker.cancel_all_orders()

    assert broker.list_open_orders() == []          # the protective stop died too
    assert len(broker.get_positions()) == 1         # ...while the position remains


# ---------------------------------------------------------------------------
# FakeBroker structurally satisfies the BrokerClient protocol (the seam contract).
# ---------------------------------------------------------------------------
def test_fake_broker_satisfies_broker_client_protocol() -> None:
    client: BrokerClient = FakeBroker()  # a static + runtime structural check
    assert client.name == "fake"

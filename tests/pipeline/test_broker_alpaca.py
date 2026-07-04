"""Tests for ``AlpacaBroker`` -- the real httpx REST client for Alpaca's trading API.

NO LIVE NETWORK in any test. Every test injects an ``httpx.Client`` wired to an
``httpx.MockTransport`` whose handler returns recorded/fake Alpaca JSON, so the client's
request shaping + response parsing is exercised end-to-end without ever touching the wire.
A ``MockTransport`` raises if a request escapes the recorded routes, so a stray real call
would fail the test rather than hit Alpaca.

The load-bearing properties asserted here:

* request shaping -- ``submit_order`` POSTs the right body (client_order_id, type="limit",
  limit_price as a string, qty, side) to ``POST /v2/orders``.
* numbers-as-strings coercion -- Alpaca returns ``filled_qty`` / ``filled_avg_price`` /
  ``qty`` / ``avg_entry_price`` as STRINGS; the parsed value types must be int / float.
* the status map -- Alpaca's order vocabulary collapses to ours
  (new/accepted/pending_new -> "new", partially_filled, filled, canceled/expired ->
  "canceled", rejected -> "rejected").
* ``is_real_money`` fail-safe -- the paper host -> False; a live host -> True; an UNKNOWN
  host -> True (an unrecognized endpoint is treated as real money).
* errors surface -- a non-2xx response raises (the LiveAdapter catches it; we don't swallow).
* auth headers -- the Alpaca key/secret headers are sent on every request.
"""

import httpx
import pytest

from swing_screener.pipeline.broker import (
    BrokerAccount,
    BrokerClient,
    BrokerOrder,
    BrokerOrderSpec,
    BrokerPosition,
)
from swing_screener.pipeline.broker_alpaca import AlpacaBroker

PAPER_HOST = "https://paper-api.alpaca.markets"


def _order_json(
    *,
    order_id: str = "39c489ad-fcb1-42b3-9f62-e14c11c62157",
    client_order_id: str = "k1",
    symbol: str = "AAPL",
    status: str = "new",
    filled_qty: str = "0",
    filled_avg_price: str | None = None,
    side: str = "buy",
) -> dict[str, object]:
    """A trimmed-but-faithful Alpaca order JSON (numbers as STRINGS, like the real API)."""
    return {
        "id": order_id,
        "client_order_id": client_order_id,
        "symbol": symbol,
        "status": status,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "qty": "10",
        "side": side,
        "type": "limit",
        "limit_price": "100.00",
        "time_in_force": "day",
    }


def _broker(handler: object, *, host: str = PAPER_HOST) -> AlpacaBroker:
    """Build an ``AlpacaBroker`` whose injected client routes through a MockTransport."""
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    client = httpx.Client(
        transport=transport,
        base_url=host,
        headers={"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"},
    )
    return AlpacaBroker(key="k", secret="s", host=host, client=client)


def _spec(
    *, client_order_id: str = "k1", symbol: str = "AAPL", qty: int = 10,
    limit_price: float | None = 100.0, stop_loss: float | None = None,
    take_profit: float | None = None,
) -> BrokerOrderSpec:
    return BrokerOrderSpec(
        client_order_id=client_order_id,
        symbol=symbol,
        side="buy",
        qty=qty,
        order_type="limit",
        limit_price=limit_price,
        time_in_force="day",
        stop_loss=stop_loss,
        take_profit=take_profit,
    )


# ---------------------------------------------------------------------------
# submit_order -> POST /v2/orders: request shaping + response parse.
# ---------------------------------------------------------------------------
def test_submit_order_posts_the_right_body_and_parses_the_response() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_order_json(status="new"))

    broker = _broker(handler)
    order = broker.submit_order(_spec(symbol="AAPL", qty=10, limit_price=100.0))

    assert seen["method"] == "POST"
    assert seen["path"] == "/v2/orders"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["client_order_id"] == "k1"
    assert body["symbol"] == "AAPL"
    assert body["side"] == "buy"
    assert body["type"] == "limit"
    assert body["time_in_force"] == "day"
    assert body["qty"] == "10"  # Alpaca wants strings
    assert body["limit_price"] == "100.0"  # a string

    assert isinstance(order, BrokerOrder)
    assert order.broker_order_id == "39c489ad-fcb1-42b3-9f62-e14c11c62157"
    assert order.client_order_id == "k1"
    assert order.status == "new"
    assert order.symbol == "AAPL"


# ---------------------------------------------------------------------------
# get_order -> GET /v2/orders/{id}: a filled order coerces strings -> int/float.
# ---------------------------------------------------------------------------
def test_get_order_parses_a_filled_order_coercing_string_numbers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/orders/oid-1"
        return httpx.Response(
            200,
            json=_order_json(
                order_id="oid-1", status="filled",
                filled_qty="10", filled_avg_price="123.45",
            ),
        )

    broker = _broker(handler)
    order = broker.get_order("oid-1")

    assert order.broker_order_id == "oid-1"
    assert order.status == "filled"
    assert order.filled_qty == 10
    assert isinstance(order.filled_qty, int)
    assert order.filled_avg_price == 123.45
    assert isinstance(order.filled_avg_price, float)


def test_get_order_filled_avg_price_is_none_when_unfilled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_order_json(status="new", filled_qty="0", filled_avg_price=None))

    broker = _broker(handler)
    order = broker.get_order("oid-x")
    assert order.filled_avg_price is None
    assert order.filled_qty == 0


# ---------------------------------------------------------------------------
# the status map: Alpaca's vocabulary -> ours.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("alpaca_status", "expected"),
    [
        ("new", "new"),
        ("accepted", "new"),
        ("pending_new", "new"),
        ("partially_filled", "partially_filled"),
        ("filled", "filled"),
        ("canceled", "canceled"),
        ("expired", "canceled"),
        ("rejected", "rejected"),
    ],
)
def test_status_mapping(alpaca_status: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_order_json(status=alpaca_status))

    broker = _broker(handler)
    assert broker.get_order("oid").status == expected


# ---------------------------------------------------------------------------
# list_open_orders -> GET /v2/orders?status=open.
# ---------------------------------------------------------------------------
def test_list_open_orders_parses_a_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/orders"
        assert request.url.params.get("status") == "open"
        return httpx.Response(
            200,
            json=[
                _order_json(order_id="o1", status="new"),
                _order_json(order_id="o2", status="partially_filled", filled_qty="3"),
            ],
        )

    broker = _broker(handler)
    orders = broker.list_open_orders()
    assert [o.broker_order_id for o in orders] == ["o1", "o2"]
    assert orders[1].status == "partially_filled"
    assert orders[1].filled_qty == 3


# ---------------------------------------------------------------------------
# get_positions -> GET /v2/positions: coerce qty/avg_entry_price from strings.
# ---------------------------------------------------------------------------
def test_get_positions_coerces_string_numbers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/positions"
        return httpx.Response(
            200,
            json=[
                {"symbol": "AAPL", "qty": "10", "avg_entry_price": "150.25"},
                {"symbol": "MSFT", "qty": "5", "avg_entry_price": "400.00"},
            ],
        )

    broker = _broker(handler)
    positions = broker.get_positions()
    assert positions == [
        BrokerPosition(symbol="AAPL", qty=10, avg_entry_price=150.25),
        BrokerPosition(symbol="MSFT", qty=5, avg_entry_price=400.0),
    ]
    assert isinstance(positions[0].qty, int)
    assert isinstance(positions[0].avg_entry_price, float)


# ---------------------------------------------------------------------------
# cancel_order / cancel_all_orders -> DELETE.
# ---------------------------------------------------------------------------
def test_cancel_order_deletes_by_id() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(204)

    broker = _broker(handler)
    broker.cancel_order("oid-9")
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/v2/orders/oid-9"


def test_cancel_all_orders_deletes_the_collection() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(207, json=[])

    broker = _broker(handler)
    broker.cancel_all_orders()
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/v2/orders"


# ---------------------------------------------------------------------------
# is_real_money: paper -> False; live -> True; unknown -> True (fail safe).
# ---------------------------------------------------------------------------
def test_is_real_money_false_for_paper_host() -> None:
    broker = _broker(lambda r: httpx.Response(200, json={}), host=PAPER_HOST)
    assert broker.is_real_money() is False


def test_is_real_money_true_for_live_host() -> None:
    broker = _broker(
        lambda r: httpx.Response(200, json={}), host="https://api.alpaca.markets")
    assert broker.is_real_money() is True


def test_is_real_money_true_for_unknown_host_fail_safe() -> None:
    broker = _broker(
        lambda r: httpx.Response(200, json={}), host="https://example.invalid")
    assert broker.is_real_money() is True


# ---------------------------------------------------------------------------
# errors surface: a non-2xx raises (the LiveAdapter catches it; we don't swallow).
# ---------------------------------------------------------------------------
def test_non_2xx_response_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    broker = _broker(handler)
    with pytest.raises(httpx.HTTPStatusError):
        broker.submit_order(_spec())


# ---------------------------------------------------------------------------
# last_close_price: the latest FILLED closing order's price, or None.
# ---------------------------------------------------------------------------
def test_last_close_price_from_recent_filled_closing_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/orders"
        assert request.url.params.get("status") == "closed"
        assert request.url.params.get("symbols") == "AAPL"
        return httpx.Response(
            200,
            json=[
                # newest first (Alpaca default direction=desc); the first FILLED wins.
                _order_json(
                    order_id="latest", symbol="AAPL", status="filled",
                    filled_qty="10", filled_avg_price="161.50", side="sell"),
                _order_json(
                    order_id="older", symbol="AAPL", status="filled",
                    filled_qty="10", filled_avg_price="150.00", side="sell"),
            ],
        )

    broker = _broker(handler)
    assert broker.last_close_price("AAPL") == 161.50


def test_last_close_price_skips_unfilled_orders() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                # a canceled order with no fill must be skipped...
                _order_json(
                    order_id="canceled", symbol="AAPL", status="canceled",
                    filled_qty="0", filled_avg_price=None),
                # ...in favor of the next FILLED one.
                _order_json(
                    order_id="filled", symbol="AAPL", status="filled",
                    filled_qty="10", filled_avg_price="142.00", side="sell"),
            ],
        )

    broker = _broker(handler)
    assert broker.last_close_price("AAPL") == 142.00


def test_last_close_price_skips_the_entry_buy() -> None:
    """The entry BUY is also a filled closed order for the symbol: without the sell-side
    filter a vanished position would book its "exit" at the entry fill price (realized_r
    == 0 regardless of the real outcome)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _order_json(
                    order_id="entry", symbol="AAPL", status="filled",
                    filled_qty="10", filled_avg_price="100.00", side="buy"),
            ],
        )

    broker = _broker(handler)
    assert broker.last_close_price("AAPL") is None  # a buy is never an exit price


def test_last_close_price_none_when_no_closing_orders() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    broker = _broker(handler)
    assert broker.last_close_price("AAPL") is None


# ---------------------------------------------------------------------------
# get_account -> GET /v2/account: coerce cash/buying_power from strings + status.
# ---------------------------------------------------------------------------
def test_get_account_parses_the_account_json_coercing_string_numbers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/account"
        return httpx.Response(
            200,
            json={
                # Alpaca returns the money fields as STRINGS.
                "cash": "12345.67",
                "buying_power": "24691.34",
                "status": "ACTIVE",
            },
        )

    broker = _broker(handler)
    account = broker.get_account()

    assert isinstance(account, BrokerAccount)
    assert account.cash == 12345.67
    assert isinstance(account.cash, float)
    assert account.buying_power == 24691.34
    assert isinstance(account.buying_power, float)
    assert account.status == "ACTIVE"


def test_get_account_surfaces_a_non_active_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"cash": "0", "buying_power": "0", "status": "ACCOUNT_UPDATED"},
        )

    broker = _broker(handler)
    account = broker.get_account()
    assert account.status == "ACCOUNT_UPDATED"
    assert account.buying_power == 0.0


# ---------------------------------------------------------------------------
# auth headers are sent on every request.
# ---------------------------------------------------------------------------
def test_auth_headers_are_sent() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("APCA-API-KEY-ID")
        seen["secret"] = request.headers.get("APCA-API-SECRET-KEY")
        return httpx.Response(200, json=_order_json())

    broker = _broker(handler)
    broker.get_order("oid")
    assert seen["key"] == "k"
    assert seen["secret"] == "s"


# ---------------------------------------------------------------------------
# the default-constructed client builds the auth headers from the secrets.
# (No network -- we never call a method, just inspect the built client.)
# ---------------------------------------------------------------------------
def test_default_construction_builds_auth_headers_from_args() -> None:
    broker = AlpacaBroker(key="kk", secret="ss", host=PAPER_HOST)
    assert broker._client.headers["APCA-API-KEY-ID"] == "kk"
    assert broker._client.headers["APCA-API-SECRET-KEY"] == "ss"
    assert broker.name == "alpaca"
    broker._client.close()


# ---------------------------------------------------------------------------
# AlpacaBroker structurally satisfies the BrokerClient protocol (the seam contract).
# ---------------------------------------------------------------------------
def test_alpaca_broker_satisfies_broker_client_protocol() -> None:
    client: BrokerClient = _broker(lambda r: httpx.Response(200, json={}))
    assert client.name == "alpaca"


def test_submit_order_sends_bracket_legs_when_the_spec_carries_them() -> None:
    """With stop_loss + take_profit set, the entry goes out as order_class=bracket and
    the venue holds the protective legs itself (money-safety: the exit used to exist
    only virtually, enforced by nothing at the venue if the screener died)."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_order_json(status="new"))

    broker = _broker(handler)
    broker.submit_order(_spec(symbol="AAPL", qty=10, limit_price=100.0,
                              stop_loss=94.0, take_profit=110.0))

    body = seen["body"]
    assert isinstance(body, dict)
    assert body["order_class"] == "bracket"
    assert body["stop_loss"] == {"stop_price": "94.0"}       # strings, like every price
    assert body["take_profit"] == {"limit_price": "110.0"}


def test_submit_order_stays_a_plain_limit_without_both_legs() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_order_json(status="new"))

    broker = _broker(handler)
    broker.submit_order(_spec(symbol="AAPL", qty=10, limit_price=100.0, stop_loss=94.0))

    body = seen["body"]
    assert isinstance(body, dict)
    assert "order_class" not in body  # one leg alone never sends a half-bracket

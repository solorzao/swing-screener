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
from swing_screener.pipeline.broker_alpaca import _ORDERS_PAGE_LIMIT, AlpacaBroker

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
    submitted_at: str = "2026-07-16T13:00:00.000000Z",
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
        "submitted_at": submitted_at,
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


# ---------------------------------------------------------------------------
# get_order_by_client_id -> GET /v2/orders:by_client_order_id (the orphan-adoption
# lookup): 200 maps like get_order; 404 (no such order) -> None; other errors surface.
# ---------------------------------------------------------------------------
def test_alpaca_get_order_by_client_id_maps_order() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["client_order_id"] = request.url.params.get("client_order_id")
        return httpx.Response(
            200,
            json=_order_json(
                order_id="oid-7", client_order_id="k1", status="filled",
                filled_qty="10", filled_avg_price="123.45",
            ),
        )

    broker = _broker(handler)
    order = broker.get_order_by_client_id("k1")

    assert seen["method"] == "GET"
    assert seen["path"] == "/v2/orders:by_client_order_id"
    assert seen["client_order_id"] == "k1"

    # mapped through _to_order exactly like get_order: status map + string coercion.
    assert order is not None
    assert order.broker_order_id == "oid-7"
    assert order.client_order_id == "k1"
    assert order.status == "filled"
    assert order.filled_qty == 10
    assert order.filled_avg_price == 123.45


def test_alpaca_get_order_by_client_id_404_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "order not found"})

    assert _broker(handler).get_order_by_client_id("missing-key") is None


def test_alpaca_get_order_by_client_id_other_errors_propagate() -> None:
    """Only a 404 means "no such order"; any other HTTP error surfaces like
    ``get_order``'s do (the LiveAdapter's best-effort adoption catches it there)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(httpx.HTTPStatusError):
        _broker(handler).get_order_by_client_id("k1")


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


def _order_page(start: int, count: int) -> list[dict[str, object]]:
    """``count`` open-order JSONs with unique ids and strictly increasing submitted_at."""
    return [
        _order_json(
            order_id=f"o{start + i}",
            submitted_at=f"2026-07-16T13:00:00.{start + i:06d}Z",
        )
        for i in range(count)
    ]


def test_list_open_orders_paginates_past_a_full_page() -> None:
    """A FULL page means more may exist: the client must ask again with the cursor
    (``direction=asc`` + ``after=<last order's submitted_at>``) until a short page.
    Unpaginated, disarm's cancel sweep would silently miss every order past the
    boundary."""
    requests: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/orders"
        requests.append(request.url.params)
        if len(requests) == 1:
            return httpx.Response(200, json=_order_page(0, _ORDERS_PAGE_LIMIT))
        return httpx.Response(200, json=_order_page(_ORDERS_PAGE_LIMIT, 2))

    broker = _broker(handler)
    orders = broker.list_open_orders()

    assert len(orders) == _ORDERS_PAGE_LIMIT + 2  # both pages, nothing dropped
    assert [o.broker_order_id for o in orders[:2]] == ["o0", "o1"]
    assert [o.broker_order_id for o in orders[-2:]] == [
        f"o{_ORDERS_PAGE_LIMIT}", f"o{_ORDERS_PAGE_LIMIT + 1}"]

    first, second = requests
    assert first.get("status") == "open"
    assert first.get("limit") == str(_ORDERS_PAGE_LIMIT)  # never Alpaca's default 50
    assert first.get("direction") == "asc"
    assert "after" not in first
    # the second ask carries the cursor: the LAST order of page one's submitted_at.
    assert second.get("after") == f"2026-07-16T13:00:00.{_ORDERS_PAGE_LIMIT - 1:06d}Z"
    assert second.get("status") == "open"


def test_list_open_orders_terminates_on_an_exactly_empty_second_page() -> None:
    """An exactly-limit page followed by an EMPTY page: one more ask, then stop
    (never spin re-asking with the same cursor)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=_order_page(0, _ORDERS_PAGE_LIMIT))
        return httpx.Response(200, json=[])

    broker = _broker(handler)
    orders = broker.list_open_orders()
    assert len(orders) == _ORDERS_PAGE_LIMIT
    assert calls["n"] == 2  # the empty page ends the loop


def test_list_open_orders_dedupes_a_boundary_duplicate() -> None:
    """If the venue re-serves the boundary order on the next page (an inclusive
    cursor), it must not come back twice -- a duplicate would double a cancel or a
    stop-restore in the disarm path."""
    calls = {"n": 0}
    page_one = _order_page(0, _ORDERS_PAGE_LIMIT)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=page_one)
        # page two re-serves the boundary order, then one genuinely new order.
        return httpx.Response(
            200, json=[page_one[-1], *_order_page(_ORDERS_PAGE_LIMIT, 1)])

    broker = _broker(handler)
    orders = broker.list_open_orders()
    ids = [o.broker_order_id for o in orders]
    assert len(ids) == len(set(ids)) == _ORDERS_PAGE_LIMIT + 1  # boundary id once
    assert ids[-1] == f"o{_ORDERS_PAGE_LIMIT}"


def test_list_open_orders_short_page_check_uses_raw_page_length() -> None:
    """A FULL page whose first element is the boundary duplicate carries only
    limit-1 NEW orders: the short-page check must count the RAW response (full ->
    keep paging), not the deduped additions, or page three is never fetched."""
    calls = {"n": 0}
    page_one = _order_page(0, _ORDERS_PAGE_LIMIT)
    # raw length == limit, but only limit-1 orders survive the dedupe.
    page_two = [page_one[-1], *_order_page(_ORDERS_PAGE_LIMIT, _ORDERS_PAGE_LIMIT - 1)]

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=page_one)
        if calls["n"] == 2:
            return httpx.Response(200, json=page_two)
        return httpx.Response(200, json=_order_page(2 * _ORDERS_PAGE_LIMIT - 1, 1))

    orders = _broker(handler).list_open_orders()
    ids = [o.broker_order_id for o in orders]
    assert calls["n"] == 3  # page two was raw-FULL, so a third ask must happen
    assert len(ids) == len(set(ids)) == 2 * _ORDERS_PAGE_LIMIT


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
# cancel_order -> DELETE by id.
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


# ---------------------------------------------------------------------------
# side / order_type parsing + the plain protective STOP submit (disarm restore).
# ---------------------------------------------------------------------------
def test_get_order_parses_side_and_order_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raw = _order_json(order_id="oid-9", side="sell")
        raw["type"] = "stop"
        return httpx.Response(200, json=raw)

    order = _broker(handler).get_order("oid-9")
    assert order.side == "sell"
    assert order.order_type == "stop"


def test_submit_plain_stop_sends_stop_price_and_no_limit() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        raw = _order_json(order_id="oid-10", side="sell")
        raw["type"] = "stop"
        return httpx.Response(200, json=raw)

    spec = BrokerOrderSpec(
        client_order_id="restop-1", symbol="NVDA", side="sell", qty=8,
        order_type="stop", limit_price=None, time_in_force="gtc", stop_price=95.0)
    _broker(handler).submit_order(spec)

    body = seen["body"]
    assert isinstance(body, dict)
    assert body["side"] == "sell"
    assert body["type"] == "stop"
    assert body["stop_price"] == "95.0"      # Alpaca wants strings
    assert body["time_in_force"] == "gtc"    # protection must not expire at EOD
    assert "limit_price" not in body
    assert "order_class" not in body         # a plain stop, not a bracket

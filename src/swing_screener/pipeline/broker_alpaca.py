"""The real :class:`~swing_screener.pipeline.broker.BrokerClient`: a thin httpx REST client
for Alpaca's trading API, pointed at the PAPER endpoint by default.

This module owns ONLY the network plumbing: it shapes our :class:`BrokerOrderSpec` into the
Alpaca REST request body, calls the trading API, and parses Alpaca's JSON back into our frozen
value types (:class:`BrokerOrder` / :class:`BrokerPosition`). It implements the Protocol from
``pipeline.broker`` so the LiveAdapter (Task 4) and ``reconcile_live`` (Task 5) talk to a real
venue through the exact same seam they drive ``FakeBroker`` through in tests.

Three things are load-bearing and easy to get wrong:

* **Numbers come back as STRINGS.** Alpaca JSON returns ``filled_qty`` / ``filled_avg_price`` /
  ``qty`` / ``avg_entry_price`` as strings (e.g. ``"0.1"``, ``"154.03"``). Every numeric field
  is coerced through ``int(float(...))`` / ``float(...)`` -- never trusted as already-numeric.
* **The status map.** Alpaca has a large order-status vocabulary; we collapse it to our five
  (``new`` / ``partially_filled`` / ``filled`` / ``canceled`` / ``rejected``) in
  :data:`_STATUS_MAP`. Unknown statuses fall back to ``"new"`` (treat-as-open is the safe
  default: the reconciler keeps watching rather than declaring a terminal state we didn't model).
* **``is_real_money`` fails safe.** Only a recognized PAPER host is reported as fake money. A
  live host OR an unrecognized host counts as real money -- so a typo'd/unknown endpoint can
  never sneak past the autonomy gate as "paper".

**Errors are NOT swallowed.** Every response goes through ``raise_for_status()``; a non-2xx
surfaces as an ``httpx.HTTPStatusError``. The LiveAdapter already catches broker exceptions and
logs ``rejected_live`` gracefully, so swallowing here would only hide failures from it.

References (verified against Alpaca's trading API docs):
- ``POST /v2/orders`` request body: ``symbol``, ``qty`` (string), ``side``, ``type``,
  ``time_in_force``, ``limit_price`` (string), ``client_order_id``.
- order JSON: ``id``, ``client_order_id``, ``symbol``, ``status``, ``filled_qty`` (string),
  ``filled_avg_price`` (string|null).
- ``GET /v2/positions`` JSON: ``symbol``, ``qty`` (string), ``avg_entry_price`` (string).
- ``GET /v2/account`` JSON: ``cash`` (string), ``buying_power`` (string), ``status`` (e.g.
  ``"ACTIVE"``).
"""

from typing import TYPE_CHECKING, Any

import httpx

from swing_screener.config_secrets import get_secret, require_secret

from .broker import (
    BrokerAccount,
    BrokerClient,
    BrokerOrder,
    BrokerOrderSpec,
    BrokerPosition,
)

if TYPE_CHECKING:
    from swing_screener.settings import Settings

#: The default (and only money-safe) host: Alpaca's paper trading sandbox.
PAPER_HOST = "https://paper-api.alpaca.markets"

#: Substring that identifies a recognized PAPER host. Anything else -> real money (fail safe).
_PAPER_MARKER = "paper-api.alpaca.markets"

_DEFAULT_TIMEOUT = httpx.Timeout(30.0)

#: Alpaca's order-status vocabulary -> ours. Statuses not listed fall back to ``"new"``
#: (treat-as-open) so the reconciler keeps watching an order in a state we didn't model.
_STATUS_MAP: dict[str, str] = {
    # open / pre-fill states -> "new"
    "new": "new",
    "accepted": "new",
    "pending_new": "new",
    "accepted_for_bidding": "new",
    "calculated": "new",
    "held": "new",
    # working, partially filled
    "partially_filled": "partially_filled",
    # filled
    "filled": "filled",
    # terminal cancels (expired is an aged-out cancel)
    "canceled": "canceled",
    "expired": "canceled",
    "done_for_day": "canceled",
    # rejected
    "rejected": "rejected",
}


class AlpacaBroker:
    """A live :class:`BrokerClient` backed by Alpaca's trading REST API.

    Construction resolves credentials through the secret seam by default
    (``SWING_ALPACA_KEY`` / ``SWING_ALPACA_SECRET`` / ``SWING_ALPACA_HOST``), but every
    dependency is injectable so tests pass an ``httpx.Client`` wired to a ``MockTransport``
    (no live network). The default host is the paper sandbox.
    """

    name = "alpaca"

    def __init__(
        self,
        *,
        key: str | None = None,
        secret: str | None = None,
        host: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        key = key or require_secret("SWING_ALPACA_KEY")
        secret = secret or require_secret("SWING_ALPACA_SECRET")
        self._host = host or get_secret("SWING_ALPACA_HOST") or PAPER_HOST
        self._client = client or httpx.Client(
            base_url=self._host,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=_DEFAULT_TIMEOUT,
        )

    # -- BrokerClient protocol ------------------------------------------------
    def submit_order(self, spec: BrokerOrderSpec) -> BrokerOrder:
        """Place a LIMIT order -- as a BRACKET when the spec carries stop/target:
        ``POST /v2/orders``.

        With ``stop_loss`` + ``take_profit`` set, the venue holds the protective stop and
        the target itself (``order_class="bracket"``): a filled position stays protected
        even if the screener dies (the exit used to exist only virtually, enforced by
        nothing at the venue). Without them, a plain limit entry (legacy). Alpaca wants
        ``qty``/prices as STRINGS; ``client_order_id`` is our idempotency key (Alpaca
        rejects a duplicate, so a retried submit can't double up).
        """
        body: dict[str, Any] = {
            "symbol": spec.symbol,
            "qty": str(spec.qty),
            "side": spec.side,
            "type": spec.order_type,
            "time_in_force": spec.time_in_force,
            "client_order_id": spec.client_order_id,
        }
        if spec.limit_price is not None:
            body["limit_price"] = str(spec.limit_price)
        if spec.stop_price is not None:
            # a plain protective STOP (the disarm restore path) -- NOT a bracket.
            body["stop_price"] = str(spec.stop_price)
        if spec.stop_loss is not None and spec.take_profit is not None:
            body["order_class"] = "bracket"
            body["stop_loss"] = {"stop_price": str(spec.stop_loss)}
            body["take_profit"] = {"limit_price": str(spec.take_profit)}
        return self._to_order(self._request_json("POST", "/v2/orders", json=body))

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        """One order's current state: ``GET /v2/orders/{id}``."""
        return self._to_order(self._request_json("GET", f"/v2/orders/{broker_order_id}"))

    def list_open_orders(self) -> list[BrokerOrder]:
        """Every order still working at the venue: ``GET /v2/orders?status=open``."""
        data = self._request_json("GET", "/v2/orders", params={"status": "open"})
        return [self._to_order(o) for o in data]

    def get_positions(self) -> list[BrokerPosition]:
        """All open positions: ``GET /v2/positions`` (qty/avg_entry_price coerced from strings)."""
        data = self._request_json("GET", "/v2/positions")
        return [
            BrokerPosition(
                symbol=p["symbol"],
                qty=int(float(p["qty"])),
                avg_entry_price=float(p["avg_entry_price"]),
            )
            for p in data
        ]

    def get_account(self) -> BrokerAccount:
        """The account snapshot: ``GET /v2/account``.

        Alpaca returns the money fields (``cash`` / ``buying_power``) as STRINGS -> coerced via
        ``float(...)``; ``status`` is the venue's account status string (e.g. ``"ACTIVE"``). The
        read-only preflight check reads this to confirm the broker is reachable + funded before
        a go-live. Errors surface (``raise_for_status``); preflight catches them, not us."""
        data = self._request_json("GET", "/v2/account")
        return BrokerAccount(
            cash=float(data["cash"]),
            buying_power=float(data["buying_power"]),
            status=data["status"],
        )

    def cancel_order(self, broker_order_id: str) -> None:
        """Cancel one resting order: ``DELETE /v2/orders/{id}``."""
        self._request("DELETE", f"/v2/orders/{broker_order_id}")

    def cancel_all_orders(self) -> None:
        """Pull every resting order (the kill switch): ``DELETE /v2/orders``."""
        self._request("DELETE", "/v2/orders")

    def is_real_money(self) -> bool:
        """Whether this client trades REAL money. Conservative: ``True`` UNLESS the host is a
        recognized PAPER host. A live host or an UNKNOWN host -> ``True`` (fail safe), so an
        unrecognized endpoint can never masquerade as paper before the autonomy gate."""
        return _PAPER_MARKER not in self._host

    def last_close_price(self, symbol: str) -> float | None:
        """The price a now-flat position last EXITED at -- the ``filled_avg_price`` of the most
        recent FILLED closing order for ``symbol``, or ``None`` if there is none.

        Approach: ``GET /v2/orders?status=closed&symbols={symbol}&direction=desc&limit=...``
        returns terminal orders newest-first; we scan for the first one that actually FILLED
        (filled_qty > 0 and a non-null filled_avg_price) and return its price. The reconciler
        sources a live exit's ``exit_price`` from here -- the BROKER owns the exit price, never a
        simulated level. Returning ``None`` (no filled closing order found) is deliberate: the
        reconciler then leaves the trade OPEN rather than guessing a price.
        """
        data = self._request_json(
            "GET",
            "/v2/orders",
            params={
                "status": "closed",
                "symbols": symbol,
                "direction": "desc",
                "limit": 100,
            },
        )
        for raw in data:
            if _STATUS_MAP.get(str(raw.get("status"))) != "filled":
                continue
            # SELL side only: the entry BUY is also a filled closed order for the symbol,
            # and without this filter a vanished position would book its "exit" at the
            # entry fill price (realized_r == 0 regardless of the real outcome).
            if str(raw.get("side")) != "sell":
                continue
            price = raw.get("filled_avg_price")
            qty = raw.get("filled_qty")
            if price is not None and qty is not None and float(qty) > 0:
                return float(price)
        return None

    # -- parsing / transport --------------------------------------------------
    def _to_order(self, raw: dict[str, Any]) -> BrokerOrder:
        """Parse an Alpaca order JSON into our :class:`BrokerOrder`.

        Maps the status through :data:`_STATUS_MAP` and coerces the string-typed numeric
        fields: ``filled_qty`` via ``int(float(...))`` (Alpaca can send fractional strings like
        ``"0.1"``; for whole-share orders this is the integer count), ``filled_avg_price`` via
        ``float(...)`` or ``None`` when unfilled.
        """
        filled_avg_price = raw.get("filled_avg_price")
        return BrokerOrder(
            broker_order_id=raw["id"],
            client_order_id=raw.get("client_order_id", ""),
            status=_STATUS_MAP.get(str(raw["status"]), "new"),
            filled_qty=int(float(raw.get("filled_qty") or 0)),
            filled_avg_price=(
                float(filled_avg_price) if filled_avg_price is not None else None
            ),
            symbol=raw["symbol"],
            # side/type distinguish entry buys from protective sell legs; the disarm
            # path keys off them. Defaults match the pre-bracket vocabulary.
            side=str(raw.get("side") or "buy"),
            order_type=str(raw.get("type") or "limit"),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Issue a request and raise on a non-2xx (errors surface; we never swallow)."""
        response = self._client.request(method, path, json=json, params=params)
        response.raise_for_status()
        return response

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """:meth:`_request` + decode the JSON body."""
        return self._request(method, path, json=json, params=params).json()


def build_broker(settings: "Settings") -> BrokerClient | None:
    """Resolve the configured broker for the live path, or None when none is configured.

    ``settings.broker == "alpaca"`` -> an :class:`AlpacaBroker` (paper sandbox by default; it
    resolves its own credentials/host through the secret seam). Any other value (the empty
    default) -> None, so the live adapter / reconcile cadence is never armed without an
    explicit broker. The single factory both the digest run and the screen run resolve their
    live broker through; tests inject a ``FakeBroker`` and never reach here.
    """
    if settings.broker == "alpaca":
        return AlpacaBroker()
    return None

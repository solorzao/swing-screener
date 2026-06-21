"""The broker seam: the narrow, injectable contract Phase-4 live execution talks through.

Phase 4's LiveAdapter (Task 4) never reaches for a real broker SDK directly; it submits
through a small :class:`BrokerClient` Protocol -- exactly like the repo injects
``smtp_send`` / ``anthropic_client`` so tests never hit a real service. The real Alpaca
HTTP client (Task 6) implements this Protocol against the network; this module owns only
the CONTRACT (the Protocol + the frozen value types) and a :class:`FakeBroker` test double.

``FakeBroker`` lives here, NOT in a test file, because it is a reusable seam: Tasks 4/5/7
import it to drive an adapter / reconciler through a full order lifecycle without a venue.
It is PURE and DETERMINISTIC -- no network, no DB, no datetime, no random -- so a test can
assert exact ids and reproducible behavior. The two load-bearing properties:

* idempotency on ``client_order_id`` -- a re-submit of the same client order id returns the
  SAME order (mirrors the execution adapters' idempotency key), so a retried submit can
  never open a second position.
* deterministic ``broker_order_id``s (``fake-0``, ``fake-1``, ...) from an incrementing
  counter -- never random, never time-based -- so determinism holds in every context.
"""

from dataclasses import dataclass, replace
from typing import Protocol

# The order lifecycle states a broker order can be in. The two "open" states (an order the
# venue may still fill) are the ones ``list_open_orders`` surfaces; the rest are terminal.
_OPEN_STATUSES = ("new", "partially_filled")


@dataclass(frozen=True)
class BrokerOrderSpec:
    """The request side: one fully-specified order to submit to a broker.

    ``client_order_id`` is OUR idempotency key handed to the broker as its client order id,
    so a retried submit collapses to the same broker order. Long-only this phase
    (``side="buy"``); ``order_type="limit"`` with a ``time_in_force="day"``."""

    client_order_id: str
    symbol: str
    side: str  # "buy" (long-only this phase)
    qty: int
    order_type: str  # "limit"
    limit_price: float | None
    time_in_force: str  # "day"


@dataclass(frozen=True)
class BrokerOrder:
    """The response side: a broker order's current state.

    ``status`` is one of ``new`` / ``partially_filled`` / ``filled`` / ``canceled`` /
    ``rejected``. ``filled_avg_price`` is None until something fills."""

    broker_order_id: str
    client_order_id: str
    status: str
    filled_qty: int
    filled_avg_price: float | None
    symbol: str


@dataclass(frozen=True)
class BrokerPosition:
    """One open position at the venue: the symbol, the held quantity, and the avg entry."""

    symbol: str
    qty: int
    avg_entry_price: float


@dataclass(frozen=True)
class BrokerAccount:
    """The account snapshot the read-only preflight check consults before a go-live.

    ``cash`` / ``buying_power`` are in account currency; ``status`` is the venue's account
    status string (e.g. ``"ACTIVE"``). Preflight reads this -- never writes it -- to confirm
    the broker is reachable + funded before a human flips to real money."""

    cash: float
    buying_power: float
    status: str


class BrokerClient(Protocol):
    """The injectable broker contract Phase-4 live execution talks through.

    A narrow seam: submit/read/cancel orders, read positions, read the account snapshot
    (``get_account`` -- the read-only preflight check confirms the broker is reachable +
    funded before a go-live), report whether this client trades REAL money (the autonomy gate
    consults ``is_real_money`` before arming), and report the price a now-closed position last
    exited at (``last_close_price`` -- the reconciler sources a live exit's ``exit_price`` from
    the BROKER through it, never a simulated level). The real Alpaca client (Task 6) and
    :class:`FakeBroker` both satisfy it."""

    name: str

    def submit_order(self, spec: BrokerOrderSpec) -> BrokerOrder: ...
    def get_order(self, broker_order_id: str) -> BrokerOrder: ...
    def list_open_orders(self) -> list[BrokerOrder]: ...
    def get_positions(self) -> list[BrokerPosition]: ...
    def get_account(self) -> BrokerAccount: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
    def cancel_all_orders(self) -> None: ...
    def is_real_money(self) -> bool: ...
    def last_close_price(self, symbol: str) -> float | None: ...


class FakeBroker:
    """An in-memory :class:`BrokerClient` for tests -- pure, deterministic, venue-free.

    State is two dicts: ``_orders`` (broker_order_id -> BrokerOrder) and ``_positions``
    (symbol -> BrokerPosition). ``submit_order`` is idempotent on ``client_order_id`` and
    assigns ids from an incrementing counter (``fake-0``, ``fake-1``, ...). The protocol
    reads (``get_order`` / ``list_open_orders`` / ``get_positions``) and the cancels behave
    like a real venue; the scripting helpers (``fill`` / ``partially_fill`` / ``reject`` /
    ``close_position``) let a caller drive an order through its lifecycle by hand."""

    name = "fake"

    def __init__(
        self,
        *,
        real_money: bool = False,
        cash: float = 100_000.0,
        buying_power: float = 100_000.0,
        status: str = "ACTIVE",
    ) -> None:
        self._real_money = real_money
        # The account snapshot get_account() reports -- defaults to a funded, ACTIVE account so
        # the read-only preflight check passes by default; the knobs let a test drive an
        # unfunded / non-ACTIVE account to exercise the NO-GO paths.
        self._account = BrokerAccount(cash=cash, buying_power=buying_power, status=status)
        self._orders: dict[str, BrokerOrder] = {}
        self._positions: dict[str, BrokerPosition] = {}
        self._next_id = 0
        # symbol -> the price the position last closed at (scripted by close_position),
        # so a test can drive a venue-side exit AT a price the reconciler reads back.
        self._closes: dict[str, float] = {}
        # client_order_id -> broker_order_id, for the submit idempotency check.
        self._by_client_id: dict[str, str] = {}
        # broker_order_id -> the originally-submitted qty, so a no-qty fill can default to
        # it (BrokerOrder carries filled_qty, not the requested qty).
        self._submitted_qty: dict[str, int] = {}

    # -- BrokerClient protocol ------------------------------------------------
    def submit_order(self, spec: BrokerOrderSpec) -> BrokerOrder:
        """Submit an order. Idempotent on ``client_order_id``: a re-submit of a known
        client order id returns the EXISTING order (no second order is created)."""
        existing_id = self._by_client_id.get(spec.client_order_id)
        if existing_id is not None:
            return self._orders[existing_id]

        broker_order_id = f"fake-{self._next_id}"
        self._next_id += 1
        order = BrokerOrder(
            broker_order_id=broker_order_id,
            client_order_id=spec.client_order_id,
            status="new",
            filled_qty=0,
            filled_avg_price=None,
            symbol=spec.symbol,
        )
        self._orders[broker_order_id] = order
        self._by_client_id[spec.client_order_id] = broker_order_id
        self._submitted_qty[broker_order_id] = spec.qty
        return order

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        return self._orders[broker_order_id]

    def list_open_orders(self) -> list[BrokerOrder]:
        """Every order still working at the venue (``new`` / ``partially_filled``)."""
        return [o for o in self._orders.values() if o.status in _OPEN_STATUSES]

    def get_positions(self) -> list[BrokerPosition]:
        return list(self._positions.values())

    def get_account(self) -> BrokerAccount:
        """The account snapshot (cash / buying_power / status), as set at construction."""
        return self._account

    def cancel_order(self, broker_order_id: str) -> None:
        self._orders[broker_order_id] = replace(
            self._orders[broker_order_id], status="canceled")

    def cancel_all_orders(self) -> None:
        for order in self.list_open_orders():
            self.cancel_order(order.broker_order_id)

    def is_real_money(self) -> bool:
        return self._real_money

    def last_close_price(self, symbol: str) -> float | None:
        """The price ``symbol`` last closed at (scripted via ``close_position``), or None if
        it was never closed (or closed without a scripted price). The reconciler reads a live
        exit's ``exit_price`` from here -- the BROKER owns the exit price, never a simulation.
        The real Alpaca client (Task 6) derives it from the closing order's filled price."""
        return self._closes.get(symbol)

    # -- test-driver helpers (the scripting surface for Tasks 4/5/7) ----------
    def fill(self, broker_order_id: str, price: float, qty: int | None = None) -> None:
        """Fully fill an order at ``price`` (``qty`` defaults to the order's quantity) and
        open/replace the corresponding position -- the way a venue fill would."""
        order = self._orders[broker_order_id]
        fill_qty = qty if qty is not None else self._submitted_qty[broker_order_id]
        self._orders[broker_order_id] = replace(
            order, status="filled", filled_qty=fill_qty, filled_avg_price=price)
        self._positions[order.symbol] = BrokerPosition(
            symbol=order.symbol, qty=fill_qty, avg_entry_price=price)

    def partially_fill(self, broker_order_id: str, price: float, qty: int) -> None:
        """Partially fill an order: it stays OPEN (``partially_filled``)."""
        order = self._orders[broker_order_id]
        self._orders[broker_order_id] = replace(
            order, status="partially_filled", filled_qty=qty, filled_avg_price=price)

    def reject(self, broker_order_id: str) -> None:
        """Mark an order rejected (terminal, not open)."""
        self._orders[broker_order_id] = replace(
            self._orders[broker_order_id], status="rejected")

    def close_position(self, symbol: str, price: float | None = None) -> None:
        """Remove a position -- simulating a venue-side exit (a sold/closed position). When a
        ``price`` is given it is recorded as the close price (``last_close_price`` reads it
        back), so a test can drive a live exit AT a known price that the reconciler sources
        from the broker. Closing without a price (the original signature) is still supported."""
        self._positions.pop(symbol, None)
        if price is not None:
            self._closes[symbol] = price

"""Live-data seams for the cockpit: a TTL quote cache and a TTL broker snapshot.

Two facts shape everything here:

* uvicorn runs sync endpoints on a threadpool and the frontend fires its first
  requests concurrently (the exact race ``api.py``'s engine cache documents), so
  the invariant is: the 60s poll must never pay a cold yfinance fetch twice.
  Both caches are single-flight -- ONE ``threading.Lock`` held ACROSS the
  upstream call. Blocking concurrent requests is the deliberate choice, and the
  worst case is honest: ``latest_closes`` walks its tickers SERIALLY with
  per-ticker retry sleeps, so a cold window over a dozen tickers with two dead
  ones holds the lock for SECONDS -- once per window, bounded by one serial
  ``latest_closes`` pass over the unknown subset -- and every quote-consuming
  endpoint queues behind it. That still beats paying the same fetch twice.
* ``data.fetch`` does NOT negative-cache upstream failures: a dead/delisted
  ticker costs ~1.5-2.5s of retry sleeps on EVERY ``latest_closes`` call. So the
  quote cache records upstream misses in a known-missing set and refuses to
  re-ask for them until the window resets -- the load-bearing rule: a dead
  ticker costs its retry sleeps at most once per TTL window.

:class:`QuoteCache` accumulates ONE merged price dict per TTL window. A request
whose tickers are all accounted for (priced or known-missing) is served from
cache; anything else triggers one upstream call for exactly the unknown subset,
merged in. On expiry the WHOLE cache resets, wholesale -- the window is anchored
at its first contributing fetch, so no price is ever served staler than one TTL.
``as_of`` is the wall-clock instant of the last upstream call that contributed
to the window; individual prices may predate ``as_of`` by up to one TTL -- it
timestamps the window's latest contribution, not each price.

:class:`BrokerSnapshot` is the same posture around the broker: the factory
answering None (no broker configured) and ANY broker exception both degrade to a
``None`` snapshot cached until the TTL -- degrade, never crash, never hammer a
down venue inside the window. ``invalidate()`` is the post-DISARM cache bust
(Task 9): after a cancel-all the UI must not render pre-disarm orders for up to
a TTL.

``clock`` is the test seam (the ``gh.py`` TTL-cache precedent): a monotonic
float supplier, defaulting to ``time.monotonic``. ``create_app`` constructs one
instance of each and parks them on ``app.state.quote_cache`` /
``app.state.broker_snapshot``; Tasks 6/9/10 consume them from there.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from swing_screener.pipeline.broker import BrokerClient, BrokerOrder, BrokerPosition


@dataclass(frozen=True, kw_only=True)
class QuoteResult:
    """One answered quote request: the requested tickers' latest closes (misses
    ABSENT from the dict, never None -- mirroring ``data.quotes.latest_closes``)
    plus the UTC instant of the last upstream call that contributed. Individual
    prices may predate ``as_of`` by up to one TTL: it timestamps the window's
    latest contribution, not each price -- render it as such."""

    prices: dict[str, float]
    as_of: datetime


class QuoteCache:
    """A single-flight TTL cache over a ``latest_closes``-shaped fetch fn (module doc).

    ``latest_closes``-shaped includes the failure posture: the fetch NEVER raises
    -- failures are simply absent from its dict (``fetch_bars`` swallows per-ticker
    errors). Nothing here catches, and that is correct ONLY under that contract: a
    seam-provided fetch that raises propagates to the caller (an endpoint 500),
    with cache state unchanged -- the merge happens after the fetch returns, and
    the ``with`` block releases the lock on the way out."""

    def __init__(
        self,
        fetch_fn: Callable[[list[str]], dict[str, float]],
        *,
        ttl_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch_fn
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        # The current window: merged prices + known-missing tickers, anchored at the
        # window's FIRST contributing fetch (None = no window yet). All four reset
        # together on expiry -- the wholesale reset the module doc promises.
        self._prices: dict[str, float] = {}
        self._missing: set[str] = set()
        self._window_start: float | None = None
        self._as_of: datetime | None = None

    def get(self, tickers: list[str]) -> QuoteResult:
        """Prices for ``tickers``: at most ONE upstream call, for exactly the
        unknown subset. Upstream misses stay absent from the result AND are
        negative-cached for the rest of the window (the load-bearing rule). The
        degenerate never-fetched case (an all-cached-nothing request on a cold
        cache) stamps ``as_of = now`` -- nothing cached means nothing stale."""
        with self._lock:
            now = self._clock()
            if self._window_start is not None and now - self._window_start >= self._ttl_s:
                self._prices = {}
                self._missing = set()
                self._window_start = None
                self._as_of = None
            unknown = [
                t for t in dict.fromkeys(tickers)  # de-duped, request order kept
                if t not in self._prices and t not in self._missing
            ]
            if unknown:
                fetched = self._fetch(unknown)
                if self._window_start is None:
                    self._window_start = now
                self._prices.update(fetched)
                self._missing.update(t for t in unknown if t not in fetched)
                self._as_of = datetime.now(UTC)
            as_of = self._as_of if self._as_of is not None else datetime.now(UTC)
            return QuoteResult(
                prices={t: self._prices[t] for t in tickers if t in self._prices},
                as_of=as_of,
            )


@dataclass(frozen=True, kw_only=True)
class Snapshot:
    """One venue read: open positions + working orders (frozen tuples -- no
    consumer can mutate what another poll is about to render) and the UTC
    instant they were read."""

    positions: tuple[BrokerPosition, ...]
    open_orders: tuple[BrokerOrder, ...]
    as_of: datetime


class BrokerSnapshot:
    """A single-flight TTL cache over the broker's positions + open orders."""

    def __init__(
        self,
        broker_factory: Callable[[], BrokerClient | None],
        *,
        ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = broker_factory
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Snapshot | None = None
        self._fetched_at: float | None = None

    def get(self) -> Snapshot | None:
        """The venue's positions + open orders, or None (no broker configured, or
        the read failed). BOTH answers are cached for the TTL: a down venue is
        probed once per window, never once per poll -- degrade, never crash."""
        with self._lock:
            now = self._clock()
            if self._fetched_at is not None and now - self._fetched_at < self._ttl_s:
                return self._cached
            self._cached = self._read()
            self._fetched_at = now
            return self._cached

    def invalidate(self) -> None:
        """Bust the cache: the next ``get`` re-reads the venue. The post-DISARM
        hook (Task 9) -- after a cancel-all, a snapshot of pre-disarm orders must
        not survive up to a full TTL."""
        with self._lock:
            self._cached = None
            self._fetched_at = None

    def _read(self) -> Snapshot | None:
        """One venue read behind the degrade posture. The factory runs per refresh,
        INSIDE the try -- it is cheap (env reads + client construction, no network
        until the reads), and a factory that raises (secret resolution) must
        degrade exactly like a venue error."""
        try:
            broker = self._factory()
            if broker is None:
                return None
            return Snapshot(
                positions=tuple(broker.get_positions()),
                open_orders=tuple(broker.list_open_orders()),
                as_of=datetime.now(UTC),
            )
        except Exception:  # the module invariant: degrade to None, never crash
            return None

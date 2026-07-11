"""cockpit.livedata: the TTL quote cache and TTL broker snapshot behind the live views.

The invariant under test: the frontend's 60s poll must never pay a cold upstream
fetch twice -- repeated gets inside a TTL window cost ONE upstream call (quotes)
or ONE factory/broker read (broker), misses and failures included, even when the
requests race on uvicorn's threadpool. No test touches yfinance, a venue, or a
database: ``fetch_fn`` / ``broker_factory`` are the seams, and the injectable
``clock`` (the gh.py precedent) drives every TTL edge deterministically.
"""

import threading
import time
from pathlib import Path

from swing_screener.cockpit.api import create_app
from swing_screener.cockpit.livedata import BrokerSnapshot, QuoteCache, Snapshot
from swing_screener.pipeline.broker import (
    BrokerClient,
    BrokerOrderSpec,
    BrokerPosition,
    FakeBroker,
)

_TTL = 600.0


class _Clock:
    """A hand-cranked monotonic clock: tests advance ``now`` to cross TTL edges."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _FetchRecorder:
    """A scripted ``latest_closes``-shaped fetch: answers from ``book`` and records
    every upstream call -- tickers absent from the book stay absent from the answer
    (the misses-ABSENT contract of data.quotes.latest_closes)."""

    def __init__(self, book: dict[str, float]) -> None:
        self.book = book
        self.calls: list[list[str]] = []

    def __call__(self, tickers: list[str]) -> dict[str, float]:
        self.calls.append(list(tickers))
        return {t: self.book[t] for t in tickers if t in self.book}


def _spec(client_order_id: str, symbol: str) -> BrokerOrderSpec:
    return BrokerOrderSpec(
        client_order_id=client_order_id, symbol=symbol, side="buy", qty=10,
        order_type="limit", limit_price=100.0, time_in_force="day",
    )


# -- QuoteCache -------------------------------------------------------------------


def test_repeated_gets_within_ttl_cost_one_upstream_call() -> None:
    fetch = _FetchRecorder({"AAPL": 150.0, "MSFT": 300.0})
    cache = QuoteCache(fetch, ttl_s=_TTL, clock=_Clock())
    first = cache.get(["AAPL", "MSFT"])
    assert first.prices == {"AAPL": 150.0, "MSFT": 300.0}
    second = cache.get(["AAPL", "MSFT"])
    assert second.prices == {"AAPL": 150.0, "MSFT": 300.0}
    assert fetch.calls == [["AAPL", "MSFT"]]


def test_ttl_expiry_resets_the_whole_cache() -> None:
    """On expiry the WHOLE window resets: the next get refetches the full requested
    set (not just some subset) -- wholesale reset, per the module contract."""
    clock = _Clock()
    fetch = _FetchRecorder({"AAPL": 150.0, "MSFT": 300.0})
    cache = QuoteCache(fetch, ttl_s=_TTL, clock=clock)
    cache.get(["AAPL", "MSFT"])
    clock.now += _TTL - 1.0
    cache.get(["AAPL", "MSFT"])
    assert len(fetch.calls) == 1  # still inside the window: served from cache
    clock.now += 1.0  # exactly TTL after the window's first fetch: stale
    fetch.book["AAPL"] = 151.0
    refreshed = cache.get(["AAPL", "MSFT"])
    assert fetch.calls == [["AAPL", "MSFT"], ["AAPL", "MSFT"]]
    assert refreshed.prices["AAPL"] == 151.0


def test_partial_request_fetches_only_the_missing_tickers() -> None:
    fetch = _FetchRecorder({"AAPL": 150.0, "MSFT": 300.0})
    cache = QuoteCache(fetch, ttl_s=_TTL, clock=_Clock())
    assert cache.get(["AAPL"]).prices == {"AAPL": 150.0}
    merged = cache.get(["AAPL", "MSFT"])
    assert merged.prices == {"AAPL": 150.0, "MSFT": 300.0}
    assert fetch.calls == [["AAPL"], ["MSFT"]]  # the merge fetched ONLY the gap


def test_dead_ticker_is_negative_cached_for_the_window() -> None:
    """The load-bearing rule: an upstream miss (dead/delisted ticker) is recorded
    as known-missing and NOT re-asked within the window -- upstream retry sleeps
    cost ~1.5-2.5s per call, so a dead ticker must cost them at most once per TTL."""
    clock = _Clock()
    fetch = _FetchRecorder({"AAPL": 150.0})
    cache = QuoteCache(fetch, ttl_s=_TTL, clock=clock)
    first = cache.get(["AAPL", "DEADTKR"])
    assert first.prices == {"AAPL": 150.0}  # the miss is ABSENT, never None
    second = cache.get(["AAPL", "DEADTKR"])
    assert second.prices == {"AAPL": 150.0}
    assert fetch.calls == [["AAPL", "DEADTKR"]]  # the miss did NOT re-fetch
    clock.now += _TTL  # a new window forgets the miss: the ticker may have revived
    cache.get(["AAPL", "DEADTKR"])
    assert fetch.calls[-1] == ["AAPL", "DEADTKR"]


def test_as_of_is_the_last_contributing_fetch() -> None:
    fetch = _FetchRecorder({"AAPL": 150.0, "MSFT": 300.0})
    cache = QuoteCache(fetch, ttl_s=_TTL, clock=_Clock())
    first = cache.get(["AAPL"])
    assert first.as_of.tzinfo is not None  # UTC-aware by contract
    cached = cache.get(["AAPL"])
    assert cached.as_of == first.as_of  # served from cache: the stamp does not move
    merged = cache.get(["AAPL", "MSFT"])
    assert merged.as_of >= first.as_of  # a contributing fetch re-stamps


def test_two_threads_racing_an_expired_ttl_fetch_once() -> None:
    """The api.py engine-cache race, replayed: uvicorn runs sync endpoints on a
    threadpool and the frontend fires its first requests concurrently. The lock is
    held ACROSS the upstream call, so two racing gets cost exactly one fetch."""
    clock = _Clock()

    class _SlowFetch(_FetchRecorder):
        def __call__(self, tickers: list[str]) -> dict[str, float]:
            time.sleep(0.05)  # widen the race window: the loser must block, not refetch
            return super().__call__(tickers)

    fetch = _SlowFetch({"AAPL": 150.0})
    cache = QuoteCache(fetch, ttl_s=_TTL, clock=clock)
    cache.get(["AAPL"])  # seed the window
    clock.now += _TTL  # expire it: both threads see a stale cache
    barrier = threading.Barrier(2)
    results: list[dict[str, float]] = []

    def hit() -> None:
        barrier.wait()
        results.append(cache.get(["AAPL"]).prices)

    threads = [threading.Thread(target=hit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(fetch.calls) == 2  # the seed + exactly ONE refresh for the race
    assert results == [{"AAPL": 150.0}, {"AAPL": 150.0}]


# -- BrokerSnapshot ---------------------------------------------------------------


class _CountingFactory:
    """A broker_factory that counts resolutions and hands out a fixed answer."""

    def __init__(self, broker: BrokerClient | None) -> None:
        self.broker = broker
        self.calls = 0

    def __call__(self) -> BrokerClient | None:
        self.calls += 1
        return self.broker


class _DownBroker(FakeBroker):
    """A venue whose position read raises -- the degrade-never-crash path."""

    def __init__(self) -> None:
        super().__init__()
        self.position_reads = 0

    def get_positions(self) -> list[BrokerPosition]:
        self.position_reads += 1
        raise RuntimeError("venue down")


def test_none_factory_answers_none_once_per_ttl() -> None:
    clock = _Clock()
    factory = _CountingFactory(None)
    snap = BrokerSnapshot(factory, ttl_s=60.0, clock=clock)
    assert snap.get() is None
    assert snap.get() is None
    assert factory.calls == 1  # the None answer is cached for the window
    clock.now += 60.0
    assert snap.get() is None
    assert factory.calls == 2


def test_broker_exception_degrades_to_none_cached_for_the_window() -> None:
    clock = _Clock()
    broker = _DownBroker()
    snap = BrokerSnapshot(_CountingFactory(broker), ttl_s=60.0, clock=clock)
    assert snap.get() is None  # degrade, never crash
    assert snap.get() is None
    assert broker.position_reads == 1  # a down venue is probed once per TTL
    clock.now += 60.0
    assert snap.get() is None
    assert broker.position_reads == 2


def test_fake_broker_happy_path_snapshot() -> None:
    broker = FakeBroker()
    open_order = broker.submit_order(_spec("c-1", "AAPL"))  # stays "new": open
    filled = broker.submit_order(_spec("c-2", "MSFT"))
    broker.fill(filled.broker_order_id, 300.0)  # position; the order leaves "open"
    snap = BrokerSnapshot(_CountingFactory(broker), ttl_s=60.0, clock=_Clock())
    got = snap.get()
    assert isinstance(got, Snapshot)
    assert got.positions == (BrokerPosition(symbol="MSFT", qty=10, avg_entry_price=300.0),)
    assert [o.broker_order_id for o in got.open_orders] == [open_order.broker_order_id]
    assert got.as_of.tzinfo is not None


def test_snapshot_cached_within_ttl_and_invalidate_busts_it() -> None:
    broker = FakeBroker()
    order = broker.submit_order(_spec("c-1", "AAPL"))
    snap = BrokerSnapshot(_CountingFactory(broker), ttl_s=60.0, clock=_Clock())
    before = snap.get()
    assert before is not None and before.positions == ()
    broker.fill(order.broker_order_id, 150.0)  # the venue moves mid-window
    cached = snap.get()
    assert cached is before  # still the cached read: the TTL has not elapsed
    snap.invalidate()  # the post-DISARM cache bust (Task 9)
    fresh = snap.get()
    assert fresh is not None
    assert fresh.positions == (BrokerPosition(symbol="AAPL", qty=10, avg_entry_price=150.0),)


# -- create_app seams -------------------------------------------------------------


def _db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"


def test_create_app_threads_livedata_seams_onto_app_state(tmp_path: Path) -> None:
    """The injected fn/factory must be the ones the app.state instances actually
    call -- Tasks 6/9/10 read app.state.quote_cache / app.state.broker_snapshot."""
    fetch = _FetchRecorder({"AAPL": 150.0})
    broker = FakeBroker()
    broker.submit_order(_spec("c-1", "AAPL"))
    app = create_app(
        _db_url(tmp_path),
        latest_closes_fn=fetch,
        broker_factory=lambda: broker,
    )
    assert app.state.quote_cache.get(["AAPL"]).prices == {"AAPL": 150.0}
    assert fetch.calls == [["AAPL"]]
    got = app.state.broker_snapshot.get()
    assert got is not None and len(got.open_orders) == 1


def test_create_app_defaults_construct_livedata_instances(tmp_path: Path) -> None:
    """Omitted kwargs still build the instances (bound over settings at factory
    time). NOT exercised beyond existence -- the default quote path would hit
    yfinance and the default broker path would resolve real credentials."""
    app = create_app(_db_url(tmp_path))
    assert isinstance(app.state.quote_cache, QuoteCache)
    assert isinstance(app.state.broker_snapshot, BrokerSnapshot)

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
from swing_screener.pipeline.broker import BrokerOrder, BrokerOrderSpec, FakeBroker


def _spec(key: str, symbol: str, **overrides: object) -> BrokerOrderSpec:
    base: dict[str, object] = dict(client_order_id=key, symbol=symbol, side="buy",
                                   qty=10, order_type="limit", limit_price=100.0,
                                   time_in_force="day")
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
    base: dict[str, object] = dict(
        created_date=date(2026, 7, 3), ticker=ticker, timeframe="1d",
        play_type="continuation", run_date=date(2026, 7, 3), account="live",
        mode="alpaca", side="buy", limit_price=100.0, shares=8, stop=stop,
        target=110.0, risk_dollars=40.0, notional=800.0, status="submitted_live",
        detail="live order",
        idempotency_key=f"{ticker}|{stop}|test")
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


class _RacingBroker(FakeBroker):
    """A rival process's sweep lands a protective stop between the FIRST
    list_open_orders scan and this process's re-submit -- the cross-process
    race (guardrail trip sweep vs cockpit /api/disarm) whose different
    client_order_id suffixes the venue's duplicate-ID rejection can't collapse."""

    def __init__(self) -> None:
        super().__init__()
        self._scans = 0

    def list_open_orders(self) -> list[BrokerOrder]:  # type: ignore[override]
        self._scans += 1
        if self._scans == 2 and "rival-stop" not in self._orders:
            # the rival's GTC stop appears AFTER the first scan.
            self._orders["rival-stop"] = BrokerOrder(
                broker_order_id="rival-stop", client_order_id="rival-disarm-NVDA",
                status="new", filled_qty=0, filled_avg_price=None,
                symbol="NVDA", side="sell", order_type="stop")
        return super().list_open_orders()


def test_ensure_stop_protection_rechecks_venue_before_each_submit() -> None:
    """THE duplicate-GTC-stop hazard (2026-07-18 red-team): if a concurrent sweep
    protects the symbol between the first scan and our submit, submitting anyway
    leaves TWO live sell stops -- position closed, then SHORTED on a margin
    account. The last-instant re-list must catch it and skip."""
    broker = _RacingBroker()
    entry = broker.submit_order(_spec("k2", "NVDA", qty=8))  # plain fill, no legs
    broker.fill(entry.broker_order_id, 100.0)
    n_specs = len(broker.submitted_specs)

    restored, unprotected = disarm.ensure_stop_protection(
        broker, {"NVDA": 95.0}.get, key_suffix="test")

    assert restored == []                                  # nothing was re-submitted...
    assert unprotected == []                               # ...and nothing cried wolf
    assert len(broker.submitted_specs) == n_specs          # NO second stop went out
    stops = [o for o in broker.list_open_orders()
             if o.side == "sell" and o.order_type == "stop"]
    assert len(stops) == 1                                 # exactly the rival's stop


class _StrictVenue(FakeBroker):
    """A venue with ALPACA's duplicate-id posture: re-submitting a known
    ``client_order_id`` is REJECTED (it raises) instead of collapsing to the existing
    order the way ``FakeBroker`` does. That difference is why the day/trip-stamped key
    was never the idempotency backstop it claimed to be against a real venue."""

    def submit_order(self, spec: BrokerOrderSpec) -> BrokerOrder:  # type: ignore[override]
        if spec.client_order_id in self._by_client_id:
            raise RuntimeError("duplicate client order id 42 (venue host redacted)")
        return super().submit_order(spec)


class _LaggingVenue(_StrictVenue):
    """...and its listing lags: the venue HOLDS our stop (``get_order_by_client_id``
    sees it) while ``list_open_orders`` has not caught up -- the same post-write lag
    the cockpit's snapshot hook handles. So the scan reads 'unprotected', the
    re-submit collides with our OWN live order, and only asking the venue by
    client_order_id can tell that apart from a real failure."""

    def list_open_orders(self) -> list[BrokerOrder]:  # type: ignore[override]
        return [o for o in super().list_open_orders() if o.order_type != "stop"]


def _naked_position(broker: FakeBroker, symbol: str, *, qty: int = 8) -> None:
    """A filled position with no bracket legs -- nothing protecting it."""
    entry = broker.submit_order(_spec(f"entry-{symbol}", symbol, qty=qty))
    broker.fill(entry.broker_order_id, 100.0)


def test_ensure_stop_protection_isolates_one_failing_submit(caplog) -> None:
    """PER-POSITION BOUNDARY: one venue rejection must not abort the pass. Before
    this, the first failure raised out of the loop and every REMAINING position was
    neither restored nor reported -- the silence looked exactly like success."""
    broker = FakeBroker(real_money=False)
    for symbol in ("AAA", "BOOM", "ZZZ"):
        _naked_position(broker, symbol)
    real_submit = broker.submit_order

    def submit(spec: BrokerOrderSpec) -> BrokerOrder:
        if spec.symbol == "BOOM" and spec.order_type == "stop":
            raise RuntimeError("venue rejected: wash-trade block (host redacted)")
        return real_submit(spec)
    broker.submit_order = submit  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR):
        restored, unprotected = disarm.ensure_stop_protection(
            broker, {"AAA": 95.0, "BOOM": 90.0, "ZZZ": 85.0}.get, key_suffix="test")

    assert restored == ["AAA", "ZZZ"]        # the walk CONTINUED past the failure
    assert unprotected == ["BOOM"]           # ...and the failure is reported, not lost
    live = {o.symbol for o in broker.list_open_orders()
            if o.side == "sell" and o.order_type == "stop"}
    assert live == {"AAA", "ZZZ"}
    loud = [r.message for r in caplog.records if "UNPROTECTED" in r.message]
    assert len(loud) == 1
    assert "BOOM" in loud[0]
    assert "broker error (RuntimeError)" in loud[0]   # class name only in the wording


def test_ensure_stop_protection_treats_a_duplicate_rejection_as_protected() -> None:
    """A duplicate-id rejection is BENIGN: that key's stop IS the protection, so the
    re-run is a no-op -- not an abort, and not a false UNPROTECTED alarm. Detected by
    asking the venue (Alpaca's duplicate message is not reliably parseable), and the
    rest of the pass still runs."""
    broker = _LaggingVenue(real_money=False)
    _naked_position(broker, "NVDA")
    _naked_position(broker, "AMD")
    # our OWN stop from an earlier pass under the SAME day-stamped key, still working
    broker.submit_order(BrokerOrderSpec(
        client_order_id="disarm-stop-NVDA-test", symbol="NVDA", side="sell", qty=8,
        order_type="stop", limit_price=None, time_in_force="gtc", stop_price=95.0))
    n_specs = len(broker.submitted_specs)

    restored, unprotected = disarm.ensure_stop_protection(
        broker, {"NVDA": 95.0, "AMD": 90.0}.get, key_suffix="test")

    assert unprotected == []                 # the duplicate never cried wolf...
    assert restored == ["AMD"]               # ...and the pass completed past it
    stops = [o for o in broker._orders.values()
             if o.side == "sell" and o.order_type == "stop"]
    assert len(stops) == 2                   # NVDA's original + AMD's new one, no dup
    # exactly ONE order was accepted this pass: the rejected duplicate never landed.
    assert [s.symbol for s in broker.submitted_specs[n_specs:]] == ["AMD"]


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

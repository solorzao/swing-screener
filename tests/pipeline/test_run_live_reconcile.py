"""The live RECONCILE cadence wired into the screen run (Task 7, Step 3).

``run_screen`` advances the shadow book each cycle; for the live book the BROKER owns
fills/exits, so when ``execution_mode="live"`` AND a broker is configured the run must call
``reconcile_live`` right where it advances positions -- so a broker fill materializes an
``account="live"`` PaperTrade and a venue close reconciles its exit, both from broker truth.

GATED: ``off`` (the default) / no broker -> NO reconcile (the live path stays dark). The
broker is the injectable ``FakeBroker`` (pure, deterministic, venue-free) threaded through the
new ``broker=`` seam, so the cadence is exercised end-to-end with no network.

Also home to the NIGHTLY STOP-PROTECTION RE-ASSERT (Task 19), the reconcile's sibling
invariant on the same live book: bracket entries go out ``time_in_force='day'`` and the
venue MAY apply that TIF to the child stop leg, so a multi-day swing hold can wake up
naked. Instead of guessing the venue's leg behaviour, the evening screen re-asserts the
invariant every night -- an open live position with no live stop gets one re-submitted
GTC at the ExecutionLog ticket's RECORDED level, and a position with no recorded level
is named UNPROTECTED for the human. Pinned here: the restore, the idempotent skip on an
already-protected position, the loud report, the ONE-ensure-pass rule when the
guardrails consult already swept this cycle, and the screen's isolation from it all.

In-memory SQLite, no network.
"""

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import guardrails_repo as gr
from swing_screener.db import repo
from swing_screener.db.models import ExecutionLog, ExitEvent, PaperTrade, Signal
from swing_screener.db.session import get_engine
from swing_screener.pipeline import disarm, run
from swing_screener.pipeline.broker import BrokerOrder, BrokerOrderSpec, FakeBroker
from swing_screener.pipeline.execution import LiveAdapter
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.settings import Limits

_NO_EXT_GATE = StrategyConfig(max_extension_atr=0.0)
NO_LIMITS = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=None)
SUBMIT = date(2024, 4, 1)


def _write_universe(tmp_path, tickers):
    p = tmp_path / "universe.csv"
    p.write_text("ticker,name,exchange\n" + "\n".join(f"{t},{t} Inc,NYSE" for t in tickers) + "\n")
    return p


def _intent():
    return OrderIntent(
        ticker="AMD", timeframe="1d", play_type="continuation",
        entry_floor=99.0, entry_ceiling=101.0, stop=94.0, target=110.0,
        conviction="high", shares=10, risk_dollars=70.0,
        edge_played="e", key_risk="", insight="i", side="long", limit_price=101.0)


def _submit_live_order(url: str, broker: FakeBroker) -> str:
    """Submit one live order through the LiveAdapter (a submitted_live ExecutionLog),
    so the next run's reconcile cadence has a working order to materialize."""
    with Session(get_engine(url)) as s:
        adapter = LiveAdapter(broker, gate_ready_fn=lambda _s: True)
        result = adapter.submit(_intent(), session=s, run_date=SUBMIT, limits=NO_LIMITS)
        s.commit()
    assert result.broker_order_id is not None
    return result.broker_order_id


def test_live_run_reconciles_a_broker_fill_into_a_live_position(tmp_path, bars, monkeypatch):
    """live + a broker, a broker fill -> run_screen materializes an account="live" PaperTrade
    from the BROKER price; a venue close on the next cycle reconciles the exit."""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "live")
    monkeypatch.setattr(run, "_fetch_all_timeframes", lambda *a, **k: {})  # empty screen

    url = f"sqlite:///{tmp_path / 'livereconcile.sqlite'}"
    broker = FakeBroker(real_money=False)
    oid = _submit_live_order(url, broker)
    broker.fill(oid, price=99.5)  # the venue fills at 99.5 (!= the intent's 101.0 limit)

    kw = dict(universe_path=_write_universe(tmp_path, ["ZZZ"]), db_url=url,
              cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts", cfg=_NO_EXT_GATE,
              broker=broker)
    run.run_screen(today=date(2024, 4, 2), **kw)

    with Session(get_engine(url)) as s:
        pt = s.scalars(select(PaperTrade).where(PaperTrade.account == "live")).one()
        assert pt.status == "open"
        assert pt.entry_price == 99.5             # the BROKER fill, not the intent's limit
        assert pt.entry_date == date(2024, 4, 2)  # materialized on the run's `today`
        log = s.scalars(select(ExecutionLog)).one()
        assert log.status == "filled_live"        # the materialize-once guard flipped it

    # next cycle: the venue closes the position -> the exit reconciles from broker truth.
    broker.close_position("AMD", price=104.0)
    run.run_screen(today=date(2024, 4, 3), **kw)

    with Session(get_engine(url)) as s:
        pt = s.scalars(select(PaperTrade).where(PaperTrade.account == "live")).one()
        assert pt.status == "closed"
        assert pt.exit_price == 104.0
        assert pt.exit_reason == "broker_close"
        assert pt.realized_r == (104.0 - 99.5) / (99.5 - 94.0)
        event = s.scalars(select(ExitEvent).where(ExitEvent.account == "live")).one()
        assert event.is_paper is False


def test_same_day_rerun_keeps_the_live_position(tmp_path, bars, monkeypatch):
    """A screen RE-RUN for the same date (job retry, manual re-run) must not delete the
    live book: the idempotency delete is research-only, because a live row is never
    re-materialized -- its ExecutionLog is already ``filled_live`` (2026-07-17 audit)."""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "live")
    monkeypatch.setattr(run, "_fetch_all_timeframes", lambda *a, **k: {})  # empty screen

    url = f"sqlite:///{tmp_path / 'rerunreconcile.sqlite'}"
    broker = FakeBroker(real_money=False)
    oid = _submit_live_order(url, broker)
    broker.fill(oid, price=99.5)

    kw = dict(universe_path=_write_universe(tmp_path, ["ZZZ"]), db_url=url,
              cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts", cfg=_NO_EXT_GATE,
              broker=broker)
    run.run_screen(today=date(2024, 4, 2), **kw)
    run.run_screen(today=date(2024, 4, 2), **kw)  # the retry: same date, log already terminal

    with Session(get_engine(url)) as s:
        pt = s.scalars(select(PaperTrade).where(PaperTrade.account == "live")).one()
        assert pt.status == "open"
        assert pt.entry_price == 99.5
        assert pt.opened_date == date(2024, 4, 2)


def _open_live_trade(*, ticker: str = "AMD") -> PaperTrade:
    """One OPEN live position (entry 50, stop 45, qty 10) the reconciler can close --
    the exposure that keeps the live book in scope after a disarm."""
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account="live", fill_status="filled", entry_date=date(2024, 3, 20),
        entry_price=50.0, stop=45.0, target=60.0, risk=5.0, status="open", qty=10)


def test_disarmed_open_exposure_still_reconciles(tmp_path, bars, monkeypatch):
    """THE disarmed-exposure pin: mode OFF but an open live position at the venue
    -> the screen still reconciles it.

    Disarming used to stop the reconcile precisely when the operator was trying
    to reduce risk, leaving the live book dark while positions sat at the venue
    (2026-07 review). ``live_sync.maybe_reconcile_live``'s gate is deliberately
    "live mode OR open exposure", and this is the screen-side half of it."""
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)   # disarmed
    monkeypatch.delenv("DIGEST_TO", raising=False)              # no trip mail from here
    monkeypatch.setattr(run, "_fetch_all_timeframes", lambda *a, **k: {})

    url = f"sqlite:///{tmp_path / 'disarmedexposure.sqlite'}"
    broker = FakeBroker(real_money=False)
    broker.close_position("AMD", price=44.0)      # the venue closed it while disarmed
    with Session(get_engine(url)) as s:
        s.add(_open_live_trade())
        s.commit()

    run.run_screen(universe_path=_write_universe(tmp_path, ["ZZZ"]), db_url=url,
                   cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
                   today=date(2024, 4, 2), cfg=_NO_EXT_GATE, broker=broker)

    with Session(get_engine(url)) as s:
        pt = s.scalars(select(PaperTrade).where(PaperTrade.account == "live")).one()
        assert pt.status == "closed"             # booked despite the mode being off
        assert pt.exit_price == 44.0
        assert pt.exit_reason == "broker_close"
        assert pt.exit_date == date(2024, 4, 2)


def test_off_mode_does_not_reconcile_even_with_a_broker(tmp_path, bars, monkeypatch):
    """off (default) + a broker handed in + NO open exposure -> NO reconcile: a filled
    broker order is left un-materialized (the live path stays dark). The gate's other
    arm (open exposure, even disarmed) is pinned by the test above."""
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)  # default off
    monkeypatch.setattr(run, "_fetch_all_timeframes", lambda *a, **k: {})

    url = f"sqlite:///{tmp_path / 'offreconcile.sqlite'}"
    broker = FakeBroker(real_money=False)
    oid = _submit_live_order(url, broker)
    broker.fill(oid, price=99.5)

    run.run_screen(universe_path=_write_universe(tmp_path, ["ZZZ"]), db_url=url,
                   cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
                   today=date(2024, 4, 2), cfg=_NO_EXT_GATE, broker=broker)

    with Session(get_engine(url)) as s:
        # NO live PaperTrade materialized; the submitted_live log is untouched.
        assert list(s.scalars(select(PaperTrade).where(PaperTrade.account == "live"))) == []
        assert s.scalars(select(ExecutionLog)).one().status == "submitted_live"


# -- the nightly stop-protection re-assert (Task 19) --------------------------

TODAY = date(2024, 4, 2)


def _kwargs(tmp_path, url, *, tickers=("ZZZ",), **extra):
    return dict(universe_path=_write_universe(tmp_path, list(tickers)), db_url=url,
                cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
                cfg=_NO_EXT_GATE, **extra)


def _restore_specs(broker: FakeBroker) -> list[BrokerOrderSpec]:
    """Every protective-stop RESTORE the run submitted (the ``disarm-stop-`` prefix
    ``ensure_stop_protection`` keys them with) -- as opposed to the bracket ENTRY."""
    return [spec for spec in broker.submitted_specs
            if spec.client_order_id.startswith("disarm-stop-")]


def _live_stops(broker: FakeBroker) -> list[BrokerOrder]:
    return [o for o in broker.list_open_orders()
            if o.side == "sell" and o.order_type == "stop"]


def _kill_the_day_stop_leg(broker: FakeBroker) -> None:
    """The failure mode this task exists for: the venue expires the bracket's child
    STOP leg at the close (it inherited the entry's ``time_in_force='day'``), leaving
    a multi-day swing hold naked overnight. The target leg keeps working -- only the
    protection died, which is exactly what makes it easy to miss."""
    stops = _live_stops(broker)
    assert len(stops) == 1, "the bracket fill should have spawned exactly one stop leg"
    broker.cancel_order(stops[0].broker_order_id)


def _venue_position(broker: FakeBroker, symbol: str, *, qty: int = 10,
                    price: float = 50.0) -> None:
    """An OPEN venue position with NO protective stop and no bracket legs (a plain
    entry, filled) -- the naked hold the re-assert is supposed to notice."""
    order = broker.submit_order(BrokerOrderSpec(
        client_order_id=f"entry-{symbol}", symbol=symbol, side="buy", qty=qty,
        order_type="limit", limit_price=price, time_in_force="day"))
    broker.fill(order.broker_order_id, price=price)


def _live_ticket(session, ticker: str, *, stop: float = 45.0) -> None:
    """The ExecutionLog ticket a restore copies its level from -- written with the side
    the ADAPTERS write (``intent.side == "long"``), not the venue's "buy", so these
    tests exercise the production row shape."""
    repo.add_execution_log(
        session, created_date=SUBMIT, ticker=ticker, timeframe="1d",
        play_type="continuation", run_date=SUBMIT, account="live", mode="live",
        side="long", limit_price=50.0, shares=10, stop=stop, target=60.0,
        risk_dollars=50.0, notional=500.0, status="filled_live", detail="",
        idempotency_key=f"{ticker}-live")


def _closed_live_trade(*, ticker: str, exit_date: date) -> PaperTrade:
    """One CLOSED live trade with a broker-stamped qty: (44 - 50) * 10 = -$60 realized
    -- enough to breach a $50 daily-loss cap and trip the guardrails."""
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account="live", fill_status="filled", entry_date=date(2024, 3, 20),
        entry_price=50.0, stop=45.0, target=60.0, risk=5.0, status="closed",
        exit_date=exit_date, exit_price=44.0, realized_r=-1.2, qty=10)


def _count_ensure_passes(monkeypatch) -> list[str]:
    """Record the ``key_suffix`` of EVERY ``ensure_stop_protection`` pass in the run.

    Both bindings are patched with the same counter: the guardrail sweep reaches the
    function through ``disarm``'s module global (``run_protective_sweep`` calls it by
    name), while ``run.py`` holds its own ``from ... import ensure_stop_protection``.
    Patching both means the count is honest whichever binding the code uses."""
    seen: list[str] = []
    real = disarm.ensure_stop_protection

    def counting(broker, stop_for, *, key_suffix, dry_run=False):
        seen.append(key_suffix)
        return real(broker, stop_for, key_suffix=key_suffix, dry_run=dry_run)

    monkeypatch.setattr(disarm, "ensure_stop_protection", counting)
    monkeypatch.setattr(run, "ensure_stop_protection", counting)
    return seen


def _firing(bars):
    """A frame whose last bar fires a continuation signal (copied from test_run.py)."""
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})
    return bars(rows)


def test_evening_screen_restores_a_dead_stop_gtc(tmp_path, bars, monkeypatch):
    """THE Task-19 invariant: an open live position whose venue stop died gets one
    re-submitted -- GTC (it must survive the close the day-TIF leg did not) at the
    ExecutionLog ticket's RECORDED level, copied and never computed (North Star #4),
    under a day-stamped client_order_id so a same-evening re-run collapses at the
    venue."""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "live")
    monkeypatch.delenv("DIGEST_TO", raising=False)   # no trip expected; no mail from here
    monkeypatch.setattr(run, "_fetch_all_timeframes", lambda *a, **k: {})

    url = f"sqlite:///{tmp_path / 'reassert.sqlite'}"
    broker = FakeBroker(real_money=False)
    oid = _submit_live_order(url, broker)      # the ticket records stop=94.0
    broker.fill(oid, price=99.5)               # filled -> position + bracket legs
    _kill_the_day_stop_leg(broker)

    run.run_screen(today=TODAY, broker=broker, **_kwargs(tmp_path, url))

    assert len(_restore_specs(broker)) == 1
    spec = _restore_specs(broker)[0]
    assert (spec.symbol, spec.side, spec.order_type) == ("AMD", "sell", "stop")
    assert spec.stop_price == 94.0             # the ticket's level, copied
    assert spec.qty == 10                      # the venue's position size
    assert spec.time_in_force == "gtc"         # survives the close (the whole point)
    assert spec.client_order_id == "disarm-stop-AMD-screen-20240402"
    assert [o.symbol for o in _live_stops(broker)] == ["AMD"]  # protected again


def test_evening_screen_skips_protected_positions(tmp_path, bars, monkeypatch):
    """Idempotence at the venue: a position whose stop leg is still working needs no
    repair, so the re-assert submits NOTHING -- the nightly invariant check must not
    stack a second stop on a healthy book (two live sell stops on a margin account
    mean the position is closed and then SHORTED)."""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "live")
    monkeypatch.delenv("DIGEST_TO", raising=False)
    monkeypatch.setattr(run, "_fetch_all_timeframes", lambda *a, **k: {})

    url = f"sqlite:///{tmp_path / 'reassertskip.sqlite'}"
    broker = FakeBroker(real_money=False)
    oid = _submit_live_order(url, broker)
    broker.fill(oid, price=99.5)               # the bracket's stop leg stays alive

    run.run_screen(today=TODAY, broker=broker, **_kwargs(tmp_path, url))
    run.run_screen(today=TODAY, broker=broker, **_kwargs(tmp_path, url))  # a re-run

    assert _restore_specs(broker) == []        # nothing to repair, nothing submitted
    assert len(_live_stops(broker)) == 1       # still exactly one protective stop


def test_evening_screen_reports_unprotected_loudly(tmp_path, bars, monkeypatch, caplog):
    """No recorded level -> the position is NAMED in the log and left to the human
    (guessing a stop would violate North Star #4), no order is submitted, and the
    screen still completes."""
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)   # disarmed, exposure open
    monkeypatch.delenv("DIGEST_TO", raising=False)
    monkeypatch.setattr(run, "_fetch_all_timeframes", lambda *a, **k: {})

    url = f"sqlite:///{tmp_path / 'reassertnaked.sqlite'}"
    broker = FakeBroker(real_money=False)
    _venue_position(broker, "XYZ")             # naked at the venue...
    with Session(get_engine(url)) as s:
        s.add(_open_live_trade(ticker="XYZ"))  # ...and NO ExecutionLog ticket anywhere
        s.commit()

    with caplog.at_level(logging.INFO, logger="swing_screener.pipeline.run"):
        run.run_screen(today=TODAY, broker=broker, **_kwargs(tmp_path, url))

    assert _restore_specs(broker) == []        # never guessed a level
    assert "UNPROTECTED" in caplog.text
    assert "XYZ" in caplog.text
    assert "SCREEN_RUN_COMPLETE" in caplog.text


def test_reassert_skipped_when_consult_swept(tmp_path, bars, monkeypatch):
    """THE one-pass rule: when the guardrails consult tripped this cycle its sweep
    ALREADY ran an ensure pass, so the nightly re-assert stands down -- exactly one
    ensure pass per cycle, keyed to the TRIP (not the screen), so the conduct record
    shows one owner for tonight's venue work."""
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("DIGEST_TO", raising=False)
    monkeypatch.setattr(run, "_fetch_all_timeframes", lambda *a, **k: {})
    passes = _count_ensure_passes(monkeypatch)

    url = f"sqlite:///{tmp_path / 'reassertswept.sqlite'}"
    broker = FakeBroker(real_money=False)
    _venue_position(broker, "XYZ")             # a naked position for the sweep to fix
    with Session(get_engine(url)) as s:
        s.add(_open_live_trade(ticker="XYZ"))          # open exposure (the gate)
        s.add(_closed_live_trade(ticker="LOSE", exit_date=TODAY))   # -$60 realized
        s.commit()
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0)   # breached
        _live_ticket(s, "XYZ")

    run.run_screen(today=TODAY, broker=broker, **_kwargs(tmp_path, url))

    with Session(get_engine(url)) as s:
        g = gr.load_guardrails(s)
        assert g.state == "tripped" and g.sweep_state == "complete"
        trip_id = g.trip_id
    # ONE pass, and it belongs to the trip's sweep -- the screen's would be 'screen-...'
    assert passes == [f"guardrail-{trip_id}"]
    assert [s.client_order_id for s in _restore_specs(broker)] == [
        f"disarm-stop-XYZ-guardrail-{trip_id}"]


def test_halted_book_still_gets_the_evening_re_assert(tmp_path, bars, monkeypatch):
    """A manual HALT does NOT excuse the invariant (2026-07-25 ruling): the halt
    sweep lives in the MORNING digest's dispatch loop and the screen dispatches
    nothing, so a halted evening sweeps nothing -- leaving a stop leg that died today
    unrestored until tomorrow's digest. So 'halted' re-asserts: still exactly ONE
    ensure pass, but this time it is the SCREEN's."""
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("DIGEST_TO", raising=False)
    monkeypatch.setattr(run, "_fetch_all_timeframes", lambda *a, **k: {})
    passes = _count_ensure_passes(monkeypatch)

    url = f"sqlite:///{tmp_path / 'reasserthalted.sqlite'}"
    broker = FakeBroker(real_money=False)
    _venue_position(broker, "XYZ")             # naked: the stop leg died at the close
    entry = broker.submit_order(BrokerOrderSpec(
        client_order_id="resting-1", symbol="TSLA", side="buy", qty=5,
        order_type="limit", limit_price=200.0, time_in_force="day"))
    with Session(get_engine(url)) as s:
        s.add(_open_live_trade(ticker="XYZ"))
        s.commit()
        _live_ticket(s, "XYZ")
        assert gr.halt(s, source="test") is True   # the operator's manual brake

    run.run_screen(today=TODAY, broker=broker, **_kwargs(tmp_path, url))

    with Session(get_engine(url)) as s:
        assert gr.load_guardrails(s).state == "halted"   # still halted, never tripped
    assert passes == [f"screen-{TODAY:%Y%m%d}"]          # the SCREEN's single pass
    assert len(_restore_specs(broker)) == 1
    spec = _restore_specs(broker)[0]
    assert (spec.symbol, spec.stop_price, spec.time_in_force) == ("XYZ", 45.0, "gtc")
    # ...and the re-assert is NOT a sweep: the resting entry keeps working. Pulling it
    # is the dispatch loop's halt RESPONSE, not this invariant's business.
    assert broker.get_order(entry.broker_order_id).status == "new"


def test_reassert_failure_never_blocks_the_screen(tmp_path, bars, monkeypatch, caplog):
    """Isolation: the venue dying mid-re-assert must not cost the screen its core job
    -- the day's signals still persist and the SCREEN_RUN_COMPLETE ops marker (the
    Azure missing-run alert greps it) still logs."""
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("DIGEST_TO", raising=False)

    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)

    class _DeadVenue(FakeBroker):
        def list_open_orders(self):   # the re-assert's first call
            raise RuntimeError("venue unreachable")

    url = f"sqlite:///{tmp_path / 'reassertboom.sqlite'}"
    broker = _DeadVenue(real_money=False)
    _venue_position(broker, "XYZ")              # the hold the re-assert would check
    with Session(get_engine(url)) as s:
        s.add(_open_live_trade(ticker="XYZ"))   # open exposure -> the re-assert runs
        s.commit()

    with caplog.at_level(logging.INFO, logger="swing_screener.pipeline.run"):
        res = run.run_screen(today=TODAY, broker=broker,
                             **_kwargs(tmp_path, url, tickers=["AAPL"]))

    assert res.n_signals >= 1
    with Session(get_engine(url)) as s:
        sigs = list(s.scalars(select(Signal).where(Signal.run_date == TODAY)))
        assert any(x.ticker == "AAPL" for x in sigs)     # signals persisted
    assert "stop-protection re-assert failed" in caplog.text
    assert "SCREEN_RUN_COMPLETE" in caplog.text

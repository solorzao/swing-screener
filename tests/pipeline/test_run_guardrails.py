"""Breaker evaluation at the evening screen (Task 7): ``run_screen`` consults
the guardrails RIGHT AFTER its live reconcile, so a same-day venue stop-out --
the realized $ the reconcile just booked -- trips the daily-loss breaker the
same evening, not tomorrow morning as the digest's orders go out.

The load-bearing properties proven here:

* SAME-EVENING TRIP: live mode + a broker, a venue stop-out beyond the cap ->
  the screen's reconcile books the realized loss AND the breaker consult trips
  on it in the SAME run (state 'tripped', sweep 'complete' -- the broker in
  scope ran the sweep), with the trip event stamped ``source='screen'``.
* MODE-INDEPENDENT EVALUATION: the consult runs even with execution off and no
  broker -- the trip persists with ``sweep_state='pending'`` (respond_to_trip's
  broker=None contract) and the digest/hourly cycles own the sweep retry.
* NO RE-TRIP: an already-tripped book skips evaluation (no trip-event spam) but
  the unconditional resume still finishes a pending sweep when a broker is in
  scope.
* ISOLATION: guardrails machinery raising must never block the screen's core
  job -- signals still persist and the SCREEN_RUN_COMPLETE ops marker still
  logs.

All tmp-file sqlite + FakeBroker -- no venue, no network.
"""

import logging
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import guardrails_repo as gr
from swing_screener.db.models import (
    AgentGuardrailEvent,
    DisarmEvent,
    PaperTrade,
    Signal,
)
from swing_screener.db.session import get_engine
from swing_screener.pipeline import guardrails as gp
from swing_screener.pipeline import run
from swing_screener.pipeline.broker import BrokerOrderSpec, FakeBroker
from swing_screener.pipeline.execution import LiveAdapter
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.settings import Limits

_NO_EXT_GATE = StrategyConfig(max_extension_atr=0.0)
NO_LIMITS = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=None)
SUBMIT = date(2024, 4, 1)
TODAY = date(2024, 4, 2)


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


def _entry_spec(key: str, symbol: str) -> BrokerOrderSpec:
    return BrokerOrderSpec(client_order_id=key, symbol=symbol, side="buy", qty=10,
                           order_type="limit", limit_price=100.0, time_in_force="day")


def _closed_live_trade(*, ticker: str, exit_date: date) -> PaperTrade:
    """One CLOSED live trade with a broker-stamped qty: (44 - 50) * 10 = -$60 realized."""
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account="live", fill_status="filled", entry_date=date(2024, 3, 20),
        entry_price=50.0, stop=45.0, target=60.0, risk=5.0, status="closed",
        exit_date=exit_date, exit_price=44.0, realized_r=-1.2, qty=10,
    )


def _kwargs(tmp_path, url, **extra):
    return dict(universe_path=_write_universe(tmp_path, ["ZZZ"]), db_url=url,
                cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
                cfg=_NO_EXT_GATE, **extra)


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


def test_pipeline_run_imports_clean_in_a_fresh_interpreter():
    """The import-cycle canary: run_screen's guardrails import is LAZY because the
    cycle (replay -> run; guardrails -> preflight -> autonomy -> reflect -> replay)
    only bites when pipeline.run is the import ROOT -- and pytest collection usually
    imports notify.run first, which fully initializes reflect before run.py, so a
    future module-level `from swing_screener.pipeline import guardrails` in run.py
    would keep the SUITE green while the prod evening-screen job
    (`python -m swing_screener.pipeline.run`) died on ImportError. A fresh
    interpreter importing run.py first is the only honest probe."""
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    proc = subprocess.run(
        [sys.executable, "-c", "import swing_screener.pipeline.run"],
        env=env, cwd=root, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_evening_stop_out_trips_daily_loss_and_sweeps_with_broker(tmp_path, bars, monkeypatch):
    """The freshness loop closes SAME-EVENING: the screen's reconcile books the venue
    stop-out's realized loss, and the breaker consult right after it trips on that loss
    -- with the broker already in scope, the trip's sweep runs to 'complete'."""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "live")
    monkeypatch.setattr(run, "_fetch_all_timeframes", lambda *a, **k: {})  # empty screen

    url = f"sqlite:///{tmp_path / 'eveningtrip.sqlite'}"
    broker = FakeBroker(real_money=False)
    oid = _submit_live_order(url, broker)
    broker.fill(oid, price=99.5)                 # filled at the venue...
    broker.close_position("AMD", price=44.0)     # ...stopped out: (44 - 99.5) * 10 = -$555
    with Session(get_engine(url)) as s:
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0)

    run.run_screen(today=TODAY, broker=broker, **_kwargs(tmp_path, url))

    with Session(get_engine(url)) as s:
        # the reconcile booked the realized loss...
        pt = s.scalars(select(PaperTrade).where(PaperTrade.account == "live")).one()
        assert pt.status == "closed"
        assert pt.exit_date == TODAY
        # ...and the SAME run's breaker consult tripped on it.
        g = gr.load_guardrails(s)
        assert g.state == "tripped"
        assert g.trip_reason == "max daily loss: $-555.00 <= -$50.00"
        assert g.sweep_state == "complete"       # broker in scope -> the sweep ran
        trip = s.query(AgentGuardrailEvent).filter_by(kind="trip").one()
        assert trip.source == "screen"


def test_off_mode_breach_trips_with_pending_sweep_and_no_broker(tmp_path, bars, monkeypatch):
    """Execution off, no broker anywhere: the consult still runs (state-checked, not
    mode-gated) and the trip persists with sweep_state='pending' -- the digest/hourly
    cycles own the sweep retry (respond_to_trip's broker=None contract)."""
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)  # default off -> no broker
    monkeypatch.setattr(run, "_fetch_all_timeframes", lambda *a, **k: {})

    url = f"sqlite:///{tmp_path / 'offtrip.sqlite'}"
    with Session(get_engine(url)) as s:
        s.add(_closed_live_trade(ticker="LOSE", exit_date=TODAY))
        s.commit()
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0)

    run.run_screen(today=TODAY, **_kwargs(tmp_path, url))

    with Session(get_engine(url)) as s:
        g = gr.load_guardrails(s)
        assert g.state == "tripped"
        assert g.trip_reason == "max daily loss: $-60.00 <= -$50.00"
        assert g.sweep_state == "pending"        # no broker -> the sweep is deferred
        assert s.query(DisarmEvent).count() == 0  # nothing venue-moving ran
        assert s.query(AgentGuardrailEvent).filter_by(kind="trip").one().source == "screen"


def test_already_tripped_book_resumes_sweep_without_second_trip(tmp_path, bars, monkeypatch):
    """An already-tripped book: evaluation is SKIPPED (no trip-event spam even though
    the breach is still live), but the unconditional resume finishes the pending sweep
    now that a broker is in scope."""
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)
    monkeypatch.setattr(run, "_fetch_all_timeframes", lambda *a, **k: {})

    url = f"sqlite:///{tmp_path / 'retrip.sqlite'}"
    with Session(get_engine(url)) as s:
        # the breach is STILL live (a losing day + the cap)...
        s.add(_closed_live_trade(ticker="LOSE", exit_date=TODAY))
        s.commit()
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0)
        # ...and last night's trip is already on the books, swept by nobody.
        assert gr.trip(s, breaker="max_daily_loss_usd",
                       reason="max daily loss: $-60.00 <= -$50.00",
                       source="screen") is not None
        assert gr.load_guardrails(s).sweep_state == "pending"
    broker = FakeBroker()
    broker.submit_order(_entry_spec("rest-1", "TSLA"))  # a resting DAY entry to pull

    run.run_screen(today=TODAY, broker=broker, **_kwargs(tmp_path, url))

    with Session(get_engine(url)) as s:
        g = gr.load_guardrails(s)
        assert g.state == "tripped"
        assert g.sweep_state == "complete"       # the unconditional resume finished it
        # tripped -> evaluation skipped -> NO second trip event despite the live breach.
        assert s.query(AgentGuardrailEvent).filter_by(kind="trip").count() == 1
    assert broker.list_open_orders() == []       # the resting entry was pulled


def test_guardrails_failure_never_blocks_the_screen(tmp_path, bars, monkeypatch, caplog):
    """The screen's core job -- persisting the day's signals -- must survive the
    guardrails machinery dying: signals persist, the run returns, and the
    SCREEN_RUN_COMPLETE ops marker (the Azure missing-run alert greps for it) still
    logs."""
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)

    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)

    def _boom(session, **kw):
        # FIRST genuinely poison the transaction (the Task-6 idiom: a failed flush
        # -- created_at NOT NULL -- leaves the session inactive,
        # PendingRollbackError on every later use). A bare raise leaves the
        # session CLEAN, so this test would pass even with the except's rollback
        # deleted; the poisoned session is what proves apply_universe_metrics
        # (and the run's tail) survive a genuinely failed transaction.
        session.add(DisarmEvent(created_at=None,  # type: ignore[arg-type]
                                reason="boom", orders_cancelled=0))
        session.flush()
        raise RuntimeError("unreachable -- the flush above raises")
    # run_screen imports the guardrails module lazily (the replay->run cycle), so
    # patch the module attribute itself -- the call site resolves it at call time.
    monkeypatch.setattr(gp, "respond_to_trip", _boom)

    url = f"sqlite:///{tmp_path / 'boom.sqlite'}"
    with Session(get_engine(url)) as s:
        # a live breach, so the consult actually reaches the raising respond_to_trip.
        s.add(_closed_live_trade(ticker="LOSE", exit_date=SUBMIT))
        s.commit()
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0)

    with caplog.at_level(logging.INFO, logger="swing_screener.pipeline.run"):
        res = run.run_screen(
            universe_path=_write_universe(tmp_path, ["AAPL"]), db_url=url,
            cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
            today=SUBMIT, cfg=_NO_EXT_GATE)

    assert res.n_signals >= 1                    # the screen completed
    with Session(get_engine(url)) as s:
        sigs = list(s.scalars(select(Signal).where(Signal.run_date == SUBMIT)))
        assert any(x.ticker == "AAPL" for x in sigs)   # signals persisted
    assert "SCREEN_RUN_COMPLETE" in caplog.text  # the ops marker still logged

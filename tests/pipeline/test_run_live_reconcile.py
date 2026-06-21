"""The live RECONCILE cadence wired into the screen run (Task 7, Step 3).

``run_screen`` advances the shadow book each cycle; for the live book the BROKER owns
fills/exits, so when ``execution_mode="live"`` AND a broker is configured the run must call
``reconcile_live`` right where it advances positions -- so a broker fill materializes an
``account="live"`` PaperTrade and a venue close reconciles its exit, both from broker truth.

GATED: ``off`` (the default) / no broker -> NO reconcile (the live path stays dark). The
broker is the injectable ``FakeBroker`` (pure, deterministic, venue-free) threaded through the
new ``broker=`` seam, so the cadence is exercised end-to-end with no network.

In-memory SQLite, no network.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db.models import ExecutionLog, ExitEvent, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline import run
from swing_screener.pipeline.broker import FakeBroker
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


def test_off_mode_does_not_reconcile_even_with_a_broker(tmp_path, bars, monkeypatch):
    """off (default) + a broker handed in -> NO reconcile: a filled broker order is left
    un-materialized (the live path stays dark)."""
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

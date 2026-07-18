"""The guardrails brake + real-money mandate inside ``LiveAdapter.submit``.

The brake (``_guardrail_block``) is consulted UNCONDITIONALLY -- paper host
included, so the Stage-0 drill rehearses every trip path -- BEFORE the
real-money guard and BEFORE the venue. A refusal is a clamp: a logged
``skipped`` row with a ``guardrail: ...`` detail. Skipped is non-counting, so
the idempotency key is never burned -- a later submit (brake released)
upgrades the same row in place and places the order.

The MANDATE (``guardrails_mandate_ok``) lives INSIDE the ``is_real_money()``
block, after ``real_money_limits_ok``: real money may not dispatch with an
unset mandatory breaker; a paper host stays exempt (matching
``real_money_limits_ok``'s posture).

Breaker state is arranged through guardrails_repo's own API (edit_limits /
halt / trip) -- the real surface, never raw SQL. In-memory SQLite, no network.
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db import guardrails_repo as gr
from swing_screener.db import repo
from swing_screener.db.models import ExecutionLog, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.broker import FakeBroker
from swing_screener.pipeline.execution import LiveAdapter
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.settings import Limits, Settings

RUN = date(2026, 6, 19)
NO_LIMITS = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=None)
# every cap set -- the floor a real-money endpoint must clear (real_money_limits_ok).
FULL_CAPS = Limits(max_daily_notional=100_000.0, max_daily_loss=2.0, max_concurrent=5)


def _intent(**over: object) -> OrderIntent:
    base: dict[str, object] = dict(
        ticker="AMD", timeframe="1d", play_type="continuation",
        entry_floor=99.0, entry_ceiling=101.0, stop=94.0, target=110.0,
        conviction="high", shares=10, risk_dollars=70.0,
        edge_played="e", key_risk="", insight="i",
        side="long", limit_price=101.0,
    )
    base.update(over)
    return OrderIntent(**base)  # type: ignore[arg-type]


def _session() -> Session:
    return Session(get_engine("sqlite:///:memory:"))


def _live_settings(*, mode: str = "live", allow: bool = True) -> Settings:
    """A Settings snapshot with the execution-mode + allow-real-money locks set as asked.

    Only the two fields ``can_arm_real_money`` reads matter; the rest are filler so the
    frozen dataclass is constructible. Built directly (not via env) so the test controls the
    locks without monkeypatching the environment."""
    from pathlib import Path
    return Settings(
        db_url="sqlite:///:memory:", chart_dir=Path("."), cache_dir=Path("."),
        pdf_dir=Path("."), edge_dir=Path("."), blob_account_url=None, blob_container="c",
        key_vault_url=None,
        azure_client_id=None, acs_endpoint=None, acs_sender=None,
        deep_analysis_enabled=False, analysis_model="m", analysis_reasoning="high",
        deep_analysis_top_n=5, deep_analysis_kinds=frozenset(), analysis_max_searches=4,
        deep_analysis_max_usd=None,
        coach_enabled=False, coach_max_usd=None,
        audit_enabled=False, audit_max_usd=None,
        account_equity=None, risk_per_trade_dollars=None, risk_pct=0.01, max_shares=None,
        execution_mode=mode, max_daily_notional=None, max_daily_loss=None,
        max_concurrent=None, broker="alpaca", allow_real_money=allow,
    )


def _closed_live_trade(
    *, ticker: str, entry_price: float = 100.0, exit_price: float = 90.0,
    qty: int | None = 10, exit_date: date = RUN, realized_r: float = -1.0,
) -> PaperTrade:
    """One CLOSED live trade with a broker-stamped qty (the breakers' $ inputs)."""
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account="live", fill_status="filled", entry_date=date(2026, 6, 10),
        entry_price=entry_price, stop=95.0, target=120.0, risk=5.0, status="closed",
        exit_date=exit_date, exit_price=exit_price, realized_r=realized_r, qty=qty,
    )


def _seed_counting_live_log(s: Session, *, ticker: str, key: str) -> None:
    """One COUNTING live ExecutionLog row for RUN (feeds the trades/day breaker)."""
    repo.add_execution_log(
        s, created_date=RUN, ticker=ticker, timeframe="1d",
        play_type="continuation", run_date=RUN, account="live", mode="live",
        side="long", limit_price=100.0, shares=1, stop=95.0, target=110.0,
        risk_dollars=5.0, notional=100.0, status="submitted_live", detail="seed",
        idempotency_key=key,
    )


def _boom(_s: Session) -> bool:
    raise AssertionError("real-money gate consulted -- the brake must fire FIRST")


# ---------------------------------------------------------------------------
# the brake states: halted / tripped refuse BEFORE the venue AND before the
# real-money guard (the gate seam deliberately raises -- it must never run).
# ---------------------------------------------------------------------------
def test_halted_state_refuses_before_venue() -> None:
    with _session() as s:
        assert gr.halt(s, source="test") is True
        broker = FakeBroker(real_money=True)
        adapter = LiveAdapter(broker, settings=_live_settings(), gate_ready_fn=_boom)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=FULL_CAPS)

        assert result.status == "skipped"
        assert result.account == "live"
        assert result.detail.startswith("guardrail:")
        assert "halted" in result.detail
        assert broker.submitted_specs == []            # the venue was never touched
        row = s.query(ExecutionLog).one()
        assert row.status == "skipped"
        assert row.detail.startswith("guardrail:")


def test_tripped_state_refuses_before_venue() -> None:
    with _session() as s:
        eid = gr.trip(s, breaker="max_daily_loss_usd", reason="realized -75.00 <= -50.00",
                      source="test")
        assert eid is not None
        broker = FakeBroker(real_money=True)
        adapter = LiveAdapter(broker, settings=_live_settings(), gate_ready_fn=_boom)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=FULL_CAPS)

        assert result.status == "skipped"
        assert result.detail.startswith("guardrail:")
        assert "tripped" in result.detail
        assert "realized -75.00 <= -50.00" in result.detail   # the trip reason rides along
        assert broker.submitted_specs == []
        row = s.query(ExecutionLog).one()
        assert row.status == "skipped"


# ---------------------------------------------------------------------------
# the four breakers, each arranged through guardrails_repo's own API.
# ---------------------------------------------------------------------------
def test_trades_per_day_breaker_blocks_at_cap() -> None:
    with _session() as s:
        _seed_counting_live_log(s, ticker="NVDA", key="k-1")
        _seed_counting_live_log(s, ticker="MSFT", key="k-2")
        gr.edit_limits(s, source="test", max_trades_per_day=2)
        broker = FakeBroker(real_money=False)
        adapter = LiveAdapter(broker, settings=_live_settings(),
                              gate_ready_fn=lambda _s: True)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)

        assert result.status == "skipped"
        assert result.detail.startswith("guardrail:")
        assert "2 >= 2" in result.detail
        assert broker.submitted_specs == []
        skipped = s.query(ExecutionLog).filter_by(status="skipped").one()
        assert skipped.ticker == "AMD"


def test_daily_loss_usd_breaker() -> None:
    with _session() as s:
        # (44 - 50) * 10 = -$60 realized today, against a $50 cap.
        s.add(_closed_live_trade(ticker="LOSE", entry_price=50.0, exit_price=44.0, qty=10))
        s.commit()
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0)
        broker = FakeBroker(real_money=False)
        adapter = LiveAdapter(broker, settings=_live_settings(),
                              gate_ready_fn=lambda _s: True)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)

        assert result.status == "skipped"
        assert result.detail.startswith("guardrail:")
        assert "-60.00" in result.detail and "50.00" in result.detail
        assert broker.submitted_specs == []
        assert s.query(ExecutionLog).filter_by(status="skipped").count() == 1


def test_drawdown_breaker() -> None:
    with _session() as s:
        # one -$100 close ((90 - 100) * 10) with its ExitEvent: dd from HWM 0 = $100.
        t = _closed_live_trade(ticker="DD", entry_price=100.0, exit_price=90.0, qty=10)
        s.add(t)
        s.commit()
        repo.record_exit_event(s, is_paper=False, trade_id=t.id, tier="daily",
                               reason="stop", message="closed", created_date=RUN,
                               account="live")
        gr.edit_limits(s, source="test", max_drawdown_usd=75.0)
        broker = FakeBroker(real_money=False)
        adapter = LiveAdapter(broker, settings=_live_settings(),
                              gate_ready_fn=lambda _s: True)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)

        assert result.status == "skipped"
        assert result.detail.startswith("guardrail:")
        assert "100.00" in result.detail and "75.00" in result.detail
        assert broker.submitted_specs == []
        assert s.query(ExecutionLog).filter_by(status="skipped").count() == 1


def test_loss_streak_breaker() -> None:
    with _session() as s:
        # three consecutive losing live closes (ExitEvent order), against a halt of 3.
        for ticker in ("L1", "L2", "L3"):
            t = _closed_live_trade(ticker=ticker, realized_r=-0.5)
            s.add(t)
            s.commit()
            repo.record_exit_event(s, is_paper=False, trade_id=t.id, tier="daily",
                                   reason="stop", message="", created_date=RUN,
                                   account="live")
        gr.edit_limits(s, source="test", loss_streak_halt=3)
        broker = FakeBroker(real_money=False)
        adapter = LiveAdapter(broker, settings=_live_settings(),
                              gate_ready_fn=lambda _s: True)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)

        assert result.status == "skipped"
        assert result.detail.startswith("guardrail:")
        assert "3 >= 3" in result.detail
        assert broker.submitted_specs == []
        assert s.query(ExecutionLog).filter_by(status="skipped").count() == 1


# ---------------------------------------------------------------------------
# the brake is UNCONDITIONAL: a paper host (is_real_money() False) is blocked
# too -- the Stage-0 drill must rehearse every trip path.
# ---------------------------------------------------------------------------
def test_brake_enforced_on_paper_host_too() -> None:
    with _session() as s:
        assert gr.halt(s, source="test") is True
        broker = FakeBroker(real_money=False)
        adapter = LiveAdapter(broker, settings=_live_settings(mode="off", allow=False),
                              gate_ready_fn=_boom)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)

        assert result.status == "skipped"
        assert result.detail.startswith("guardrail:")
        assert broker.submitted_specs == []
        assert s.query(ExecutionLog).filter_by(status="skipped").count() == 1


# ---------------------------------------------------------------------------
# the MANDATE: real money may not dispatch with an unset mandatory breaker --
# even when all three locks pass and every Limits cap is set.
# ---------------------------------------------------------------------------
def test_unset_mandatory_breaker_refuses_real_money() -> None:
    with _session() as s:
        # two of the three mandatory breakers set; max_drawdown_usd left unset.
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0, max_trades_per_day=5)
        broker = FakeBroker(real_money=True)
        adapter = LiveAdapter(broker, settings=_live_settings(mode="live", allow=True),
                              gate_ready_fn=lambda _s: True)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=FULL_CAPS)

        assert result.status == "rejected"
        assert result.detail == "max_drawdown_usd is not set"   # named, concrete cause
        assert broker.submitted_specs == []
        rejected = s.query(ExecutionLog).filter_by(status="rejected_live").one()
        assert rejected.detail == "max_drawdown_usd is not set"


def test_paper_host_exempt_from_mandate() -> None:
    with _session() as s:
        # default guardrails row: state ok, EVERY breaker unset -- a paper host
        # still reaches the venue (the mandate binds real money only).
        broker = FakeBroker(real_money=False)
        adapter = LiveAdapter(broker, settings=_live_settings(mode="off", allow=False),
                              gate_ready_fn=_boom)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)

        assert result.status == "submitted_live"
        assert result.broker_order_id == "fake-0"
        assert len(broker.list_open_orders()) == 1


# ---------------------------------------------------------------------------
# a guardrail skip never burns the idempotency key: it is non-counting, and a
# later submit (brake released) upgrades the SAME row in place and places the
# order the reconciler will scan for.
# ---------------------------------------------------------------------------
def test_guardrail_skip_is_non_counting_and_upgradeable() -> None:
    with _session() as s:
        assert gr.halt(s, source="test") is True
        broker = FakeBroker(real_money=False)
        adapter = LiveAdapter(broker, settings=_live_settings(),
                              gate_ready_fn=lambda _s: True)

        first = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        assert first.status == "skipped"
        assert first.detail.startswith("guardrail:")
        assert broker.submitted_specs == []
        # non-counting: the skip never loads against the trades/day breaker.
        assert gr.trades_today(s, run_date=RUN) == 0

        assert gr.clear_halt(s, source="test") is True
        second = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        assert second.status == "submitted_live"
        assert second.broker_order_id == "fake-0"
        assert len(broker.submitted_specs) == 1

        # ONE row (unique key), upgraded in place WITH the broker id -- exactly
        # what the reconciler scans for.
        row = s.query(ExecutionLog).one()
        assert row.status == "submitted_live"
        assert row.broker_order_id == "fake-0"

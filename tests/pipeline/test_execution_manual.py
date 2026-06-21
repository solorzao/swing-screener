"""The execution adapters (``pipeline.execution``): the order-ticket ("manual")
adapter, the NoOp ("off") adapter, and the in-code hard-limit clamp.

Money never moves here: the manual adapter only RECORDS a ticket (no paper
position opens). The load-bearing safety is (1) the limit checks live INSIDE
``submit`` -- never trusting the caller -- and clamp by logging a ``skipped`` row
that opens nothing; and (2) the idempotency key makes a duplicate submit a no-op.

In-memory SQLite, no network.
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db.models import ExecutionLog, PaperTrade
from swing_screener.db.repo import add_execution_log
from swing_screener.db.session import get_engine
from swing_screener.pipeline.execution import (
    ManualAdapter,
    NoOpAdapter,
    idempotency_key,
    notional,
)
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.settings import Limits

RUN = date(2026, 6, 19)
NO_LIMITS = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=None)


def _intent(**over: object) -> OrderIntent:
    base: dict[str, object] = dict(
        ticker="AMD", timeframe="1d", play_type="continuation",
        entry_floor=99.0, entry_ceiling=101.0, stop=95.0, target=110.0,
        conviction="high", shares=10, risk_dollars=60.0,
        edge_played="e", key_risk="", insight="i",
        side="long", limit_price=101.0,
    )
    base.update(over)
    return OrderIntent(**base)  # type: ignore[arg-type]


def _session() -> Session:
    return Session(get_engine("sqlite:///:memory:"))


# ---------------------------------------------------------------------------
# helpers: idempotency_key + notional
# ---------------------------------------------------------------------------
def test_idempotency_key_stable_and_pick_scoped() -> None:
    a = idempotency_key(_intent(), RUN)
    assert a == idempotency_key(_intent(), RUN)  # same intent x run -> same key
    assert a != idempotency_key(_intent(ticker="NVDA"), RUN)  # different pick
    assert a != idempotency_key(_intent(), date(2026, 6, 20))  # different run
    assert len(a) == 40  # sha1 hexdigest


def test_notional_is_shares_times_limit_price() -> None:
    assert notional(_intent(shares=10, limit_price=101.0)) == 1010.0


# ---------------------------------------------------------------------------
# ManualAdapter: records the ticket with the order spec, status="recorded".
# ---------------------------------------------------------------------------
def test_manual_records_ticket_with_order_spec() -> None:
    with _session() as s:
        adapter = ManualAdapter()
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        assert result.status == "recorded"
        assert result.account != "paper"  # never looks like a paper fill
        assert result.trade_id is None  # opens NO position
        row = s.query(ExecutionLog).one()
        assert row.status == "recorded"
        assert row.mode == "manual"
        assert row.side == "long"
        assert row.limit_price == 101.0
        assert row.shares == 10
        assert row.stop == 95.0
        assert row.target == 110.0
        assert row.notional == 1010.0
        assert row.account == result.account
        assert row.run_date == RUN
        # opens no paper position.
        assert s.query(PaperTrade).count() == 0


def test_manual_duplicate_submit_is_noop_one_row() -> None:
    with _session() as s:
        adapter = ManualAdapter()
        first = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        second = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        assert s.query(ExecutionLog).count() == 1  # the unique key collapses the re-send
        assert second.status == "recorded"  # still coherent
        assert second.account == first.account


# ---------------------------------------------------------------------------
# the in-code hard-limit clamp: each cap blocks at/over its edge and logs a
# `skipped` row with the reason, opening nothing. None caps never block.
# ---------------------------------------------------------------------------
def test_notional_cap_blocks_over_edge_and_logs_skipped() -> None:
    with _session() as s:
        # seed prior recorded notional near the cap; this intent (1010) pushes over.
        add_execution_log(
            s, created_date=RUN, ticker="X", timeframe="1d", play_type="continuation",
            run_date=RUN, account="research", mode="manual", side="long",
            limit_price=10.0, shares=10, stop=9.0, target=12.0, risk_dollars=10.0,
            notional=300.0, status="recorded", detail="seed", idempotency_key="seed-notional",
        )
        limits = Limits(max_daily_notional=1000.0, max_daily_loss=None, max_concurrent=None)
        result = ManualAdapter().submit(_intent(), session=s, run_date=RUN, limits=limits)
        assert result.status == "skipped"
        assert "notional" in result.detail.lower()
        # the over-cap intent is logged skipped (audit) but opens/records no order ticket.
        recorded = s.query(ExecutionLog).filter_by(status="recorded").count()
        assert recorded == 1  # only the seed; the new intent did NOT record
        skipped = s.query(ExecutionLog).filter_by(status="skipped").one()
        assert skipped.ticker == "AMD"
        assert s.query(PaperTrade).count() == 0


def test_max_concurrent_blocks_at_edge_and_logs_skipped() -> None:
    with _session() as s:
        # one open position in this account already AT the cap of 1.
        s.add(PaperTrade(ticker="X", timeframe="1d", horizon="medium", account="manual",
                         signal_score=0.5, rank=1, fill_status="filled", status="open",
                         stop=9.0, target=12.0, risk=1.0))
        s.commit()
        limits = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=1)
        result = ManualAdapter().submit(_intent(), session=s, run_date=RUN, limits=limits)
        assert result.status == "skipped"
        assert "concurrent" in result.detail.lower() or "position" in result.detail.lower()
        assert s.query(ExecutionLog).filter_by(status="recorded").count() == 0
        skipped = s.query(ExecutionLog).filter_by(status="skipped").one()
        assert skipped.ticker == "AMD"


def test_daily_loss_circuit_breaker_blocks_at_edge() -> None:
    with _session() as s:
        # closed account trades that exited today summing realized_r to -2.0 (the cap).
        for r in (-1.5, -0.5):
            s.add(PaperTrade(ticker="X", timeframe="1d", horizon="medium", account="manual",
                             signal_score=0.5, rank=1, fill_status="filled", status="closed",
                             stop=9.0, target=12.0, risk=1.0, exit_date=RUN, realized_r=r))
        s.commit()
        limits = Limits(max_daily_notional=None, max_daily_loss=2.0, max_concurrent=None)
        result = ManualAdapter().submit(_intent(), session=s, run_date=RUN, limits=limits)
        assert result.status == "skipped"
        assert "loss" in result.detail.lower()
        assert s.query(ExecutionLog).filter_by(status="recorded").count() == 0


def test_daily_loss_only_counts_this_day_and_this_account() -> None:
    with _session() as s:
        # a big loss on a DIFFERENT day, and a big loss on a DIFFERENT account today:
        # neither must trip the breaker.
        s.add(PaperTrade(ticker="Y", timeframe="1d", horizon="medium", account="manual",
                         signal_score=0.5, rank=1, fill_status="filled", status="closed",
                         stop=9.0, target=12.0, risk=1.0, exit_date=date(2026, 6, 18),
                         realized_r=-5.0))
        s.add(PaperTrade(ticker="Z", timeframe="1d", horizon="medium", account="research",
                         signal_score=0.5, rank=1, fill_status="filled", status="closed",
                         stop=9.0, target=12.0, risk=1.0, exit_date=RUN, realized_r=-5.0))
        s.commit()
        limits = Limits(max_daily_notional=None, max_daily_loss=2.0, max_concurrent=None)
        result = ManualAdapter().submit(_intent(), session=s, run_date=RUN, limits=limits)
        assert result.status == "recorded"  # neither off-day nor off-account loss counts


def test_none_caps_never_block() -> None:
    with _session() as s:
        # a heap of prior notional + open positions + losses; with all caps None nothing blocks.
        add_execution_log(
            s, created_date=RUN, ticker="X", timeframe="1d", play_type="continuation",
            run_date=RUN, account="manual", mode="manual", side="long", limit_price=100.0,
            shares=1000, stop=9.0, target=12.0, risk_dollars=10.0, notional=100000.0,
            status="recorded", detail="seed", idempotency_key="seed-none",
        )
        result = ManualAdapter().submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        assert result.status == "recorded"


# ---------------------------------------------------------------------------
# NoOpAdapter ("off"): writes nothing, returns skipped. Today's default behavior.
# ---------------------------------------------------------------------------
def test_noop_writes_nothing_and_returns_skipped() -> None:
    with _session() as s:
        result = NoOpAdapter().submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        assert result.status == "skipped"
        assert result.account == "research"
        assert s.query(ExecutionLog).count() == 0
        assert s.query(PaperTrade).count() == 0


def test_adapter_names() -> None:
    assert NoOpAdapter().name == "off"
    assert ManualAdapter().name == "manual"

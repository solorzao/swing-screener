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
def _seed_manual_notional(s: Session, notional: float, key: str) -> None:
    """Seed a PRIOR recorded ExecutionLog under account="manual" -- the account the
    manual adapter actually sums for its per-day notional cap. The notional check is
    cumulative: booked (this) + the new intent's own notional is what's compared to the
    cap. (Seeding "research" here would contribute 0 to the manual sum -- the bug the
    old test had, which let the new intent's OWN notional carry the assertion.)"""
    add_execution_log(
        s, created_date=RUN, ticker="X", timeframe="1d", play_type="continuation",
        run_date=RUN, account="manual", mode="manual", side="long",
        limit_price=10.0, shares=int(notional // 10), stop=9.0, target=12.0,
        risk_dollars=10.0, notional=notional, status="recorded", detail="seed",
        idempotency_key=key,
    )


def test_notional_cap_blocks_on_cumulative_manual_sum_over_edge() -> None:
    """The CUMULATIVE cap: an intent whose OWN notional is UNDER the cap is still blocked
    once booked + this_order EXCEEDS it -- proving _limit_block sums the booked manual
    notional, not just the intent in isolation. cap=1000, booked=600, intent=500
    (50 sh @ 10.00) -> 600+500=1100 > 1000 -> skipped."""
    with _session() as s:
        _seed_manual_notional(s, 600.0, "seed-notional")
        limits = Limits(max_daily_notional=1000.0, max_daily_loss=None, max_concurrent=None)
        # intent notional 500 (< cap 1000): would PASS in isolation; blocked only via booked.
        intent = _intent(shares=50, limit_price=10.0)
        assert notional(intent) == 500.0  # below the cap on its own
        result = ManualAdapter().submit(intent, session=s, run_date=RUN, limits=limits)
        assert result.status == "skipped"
        assert "notional" in result.detail.lower()
        # the cumulative sum (1100) is what's reported, not the bare intent notional.
        assert "1100" in result.detail
        # the over-cap intent is logged skipped (audit) but opens/records no order ticket.
        recorded = s.query(ExecutionLog).filter_by(status="recorded").count()
        assert recorded == 1  # only the seed; the new intent did NOT record
        skipped = s.query(ExecutionLog).filter_by(status="skipped").one()
        assert skipped.ticker == "AMD"
        assert s.query(PaperTrade).count() == 0


def test_notional_cap_allows_at_edge_records() -> None:
    """The AT-cap boundary is INCLUSIVE (the check is ``>``, not ``>=``): booked + this
    order == cap is ALLOWED and records. cap=1000, booked=500, intent=500 -> 1000 == 1000
    -> recorded. (One off-by-one to ``>=`` would wrongly block this.)"""
    with _session() as s:
        _seed_manual_notional(s, 500.0, "seed-notional")
        limits = Limits(max_daily_notional=1000.0, max_daily_loss=None, max_concurrent=None)
        intent = _intent(shares=50, limit_price=10.0)  # notional 500 -> 500+500 == cap
        result = ManualAdapter().submit(intent, session=s, run_date=RUN, limits=limits)
        assert result.status == "recorded"
        # both the seed and the at-cap order recorded; nothing skipped.
        assert s.query(ExecutionLog).filter_by(status="recorded").count() == 2
        assert s.query(ExecutionLog).filter_by(status="skipped").count() == 0


def test_notional_cap_ignores_other_accounts_research_does_not_count() -> None:
    """The cap sums ONLY the manual account: a large prior notional booked under
    account="research" must NOT count toward the manual cap. booked(manual)=0, so an
    intent whose own notional is under the cap records despite the research seed -- this
    is the teeth that the old test lacked (it seeded research and still 'passed' purely
    on the intent's own 1010 > 1000)."""
    with _session() as s:
        add_execution_log(
            s, created_date=RUN, ticker="X", timeframe="1d", play_type="continuation",
            run_date=RUN, account="research", mode="manual", side="long",
            limit_price=10.0, shares=900, stop=9.0, target=12.0, risk_dollars=10.0,
            notional=9000.0, status="recorded", detail="seed", idempotency_key="seed-research",
        )
        limits = Limits(max_daily_notional=1000.0, max_daily_loss=None, max_concurrent=None)
        intent = _intent(shares=50, limit_price=10.0)  # 500, under cap; manual booked is 0
        result = ManualAdapter().submit(intent, session=s, run_date=RUN, limits=limits)
        assert result.status == "recorded"  # research notional is invisible to the manual cap


def _open_manual_positions(s: Session, n: int) -> None:
    for _ in range(n):
        s.add(PaperTrade(ticker="X", timeframe="1d", horizon="medium", account="manual",
                         signal_score=0.5, rank=1, fill_status="filled", status="open",
                         stop=9.0, target=12.0, risk=1.0))
    s.commit()


def test_max_concurrent_blocks_at_edge_and_logs_skipped() -> None:
    with _session() as s:
        # two open positions in this account already AT the cap of 2 (open_count >= cap).
        _open_manual_positions(s, 2)
        limits = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=2)
        result = ManualAdapter().submit(_intent(), session=s, run_date=RUN, limits=limits)
        assert result.status == "skipped"
        assert "concurrent" in result.detail.lower() or "position" in result.detail.lower()
        assert s.query(ExecutionLog).filter_by(status="recorded").count() == 0
        skipped = s.query(ExecutionLog).filter_by(status="skipped").one()
        assert skipped.ticker == "AMD"


def test_max_concurrent_allows_just_under_edge_records() -> None:
    """The other side of the boundary: open_count == cap-1 is ALLOWED (the check is
    ``>=``). cap=2, one open manual position -> 1 < 2 -> records. Pins that the cap does
    not block a slot that is still free."""
    with _session() as s:
        _open_manual_positions(s, 1)  # one short of the cap of 2
        limits = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=2)
        result = ManualAdapter().submit(_intent(), session=s, run_date=RUN, limits=limits)
        assert result.status == "recorded"
        assert s.query(ExecutionLog).filter_by(status="recorded").one().ticker == "AMD"
        assert s.query(ExecutionLog).filter_by(status="skipped").count() == 0


def _close_manual_losses(s: Session, *rs: float) -> None:
    for r in rs:
        s.add(PaperTrade(ticker="X", timeframe="1d", horizon="medium", account="manual",
                         signal_score=0.5, rank=1, fill_status="filled", status="closed",
                         stop=9.0, target=12.0, risk=1.0, exit_date=RUN, realized_r=r))
    s.commit()


def test_daily_loss_circuit_breaker_blocks_at_edge() -> None:
    with _session() as s:
        # closed account trades that exited today summing realized_r to -2.0 (the cap):
        # day_r == -max_daily_loss -> the breaker trips (the check is ``<=``).
        _close_manual_losses(s, -1.5, -0.5)
        limits = Limits(max_daily_notional=None, max_daily_loss=2.0, max_concurrent=None)
        result = ManualAdapter().submit(_intent(), session=s, run_date=RUN, limits=limits)
        assert result.status == "skipped"
        assert "loss" in result.detail.lower()
        assert s.query(ExecutionLog).filter_by(status="recorded").count() == 0


def test_daily_loss_breaker_allows_just_under_edge_records() -> None:
    """The other side of the breaker: a day_r just ABOVE -cap is ALLOWED. cap=2.0,
    summed realized R = -1.99 (> -2.0) -> records. Pins that the breaker only trips once
    the day's loss reaches the threshold, not a hair before it."""
    with _session() as s:
        _close_manual_losses(s, -1.5, -0.49)  # sums to -1.99, just above -2.0
        limits = Limits(max_daily_notional=None, max_daily_loss=2.0, max_concurrent=None)
        result = ManualAdapter().submit(_intent(), session=s, run_date=RUN, limits=limits)
        assert result.status == "recorded"
        assert s.query(ExecutionLog).filter_by(status="recorded").one().ticker == "AMD"
        assert s.query(ExecutionLog).filter_by(status="skipped").count() == 0


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

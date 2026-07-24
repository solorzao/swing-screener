"""The paper execution adapter (``pipeline.execution.PaperAdapter``).

Unlike the manual adapter (which only RECORDS a ticket), the paper adapter OPENS a
simulated position: one FILLED ``PaperTrade`` tagged ``account="paper"`` from the
intent's fixed levels. The load-bearing claim is that NO new lifecycle code is needed
-- the EXISTING shadow stepper (``advance_open`` / ``evaluate_exit``) fills, trails,
and closes the paper trade exactly as it does the research grid. The ``account="paper"``
tag keeps it out of every research aggregate (the leaderboards + analyst calibration).

In-memory SQLite, no network.
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db.models import ExecutionLog, ExitEvent, PaperTrade
from swing_screener.db.repo import load_closed_paper_trades, load_open_paper_trades
from swing_screener.db.session import get_engine
from swing_screener.pipeline.execution import PaperAdapter
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.pipeline.shadow import advance_open
from swing_screener.settings import Limits

RUN = date(2026, 6, 19)
NEXT = date(2026, 6, 22)
NO_LIMITS = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=None)
CFG = StrategyConfig()


def _intent(**over: object) -> OrderIntent:
    base: dict[str, object] = {
        "ticker": "AMD", "timeframe": "1d", "play_type": "continuation",
        "entry_floor": 99.0, "entry_ceiling": 101.0, "stop": 94.0, "target": 110.0,
        "conviction": "high", "shares": 10, "risk_dollars": 70.0,
        "edge_played": "e", "key_risk": "", "insight": "i",
        "side": "long", "limit_price": 101.0,
    }
    base.update(over)
    return OrderIntent(**base)  # type: ignore[arg-type]


def _session() -> Session:
    return Session(get_engine("sqlite:///:memory:"))


def _get(s: Session, trade_id: int | None) -> PaperTrade:
    """Fetch a paper trade by id, asserting it exists (narrows ``| None`` for mypy)."""
    pt = s.get(PaperTrade, trade_id)
    assert pt is not None
    return pt


# ---------------------------------------------------------------------------
# the happy path: one FILLED account="paper" PaperTrade + a filled_paper log.
# ---------------------------------------------------------------------------
def test_submit_opens_one_filled_paper_position() -> None:
    with _session() as s:
        result = PaperAdapter().submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        assert result.status == "filled_paper"
        assert result.account == "paper"

        pt = s.query(PaperTrade).one()
        assert result.trade_id == pt.id  # the result carries the opened trade's id
        assert pt.account == "paper"
        assert pt.status == "open"
        assert pt.fill_status == "filled"
        assert pt.arm == "baseline" and pt.variant == "default"
        assert pt.entry_price == 101.0           # == limit_price
        assert pt.stop == 94.0 and pt.target == 110.0  # the intent's levels
        assert pt.risk == 101.0 - 94.0           # limit_price - stop, strictly positive
        assert pt.entry_date == RUN and pt.opened_date == RUN
        assert pt.signal_id is None              # intents aren't 1:1 with a persisted signal
        # the runner-state fields the stepper reads must be initialised concretely.
        assert pt.hold_bars == 0
        assert pt.remaining_frac == 1.0
        assert pt.partial_done is False
        assert pt.high_water == pt.entry_price

        log = s.query(ExecutionLog).one()
        assert log.status == "filled_paper"
        assert log.account == "paper" and log.mode == "paper"
        assert log.side == "long" and log.limit_price == 101.0 and log.shares == 10
        assert log.stop == 94.0 and log.target == 110.0
        assert log.notional == 1010.0


# ---------------------------------------------------------------------------
# the INTEGRATION test (load-bearing): the EXISTING stepper closes the paper
# trade -- proving zero new lifecycle code. Target case + a separate stop case.
# ---------------------------------------------------------------------------
def test_existing_stepper_closes_paper_trade_on_target() -> None:
    with _session() as s:
        result = PaperAdapter().submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        # advance with a LATER bar that pierces the target 110.0 -> the stepper exits it.
        bar = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False}
        advance_open(s, {("AMD", "1d"): bar}, CFG, today=NEXT)

        closed = _get(s, result.trade_id)
        assert closed.status == "closed"
        assert closed.exit_reason == "target" and closed.exit_price == 110.0
        assert closed.realized_r == (110.0 - 101.0) / (101.0 - 94.0)  # +R, set by the stepper
        assert load_open_paper_trades(s) == []

        # the recorded exit event carries the closing trade's account ("paper"), so the
        # dashboard's account facet can separate intent-book exits from research-grid ones.
        ev = s.query(ExitEvent).filter_by(trade_id=closed.id).one()
        assert ev.account == "paper"


def test_existing_stepper_closes_paper_trade_on_stop() -> None:
    with _session() as s:
        result = PaperAdapter().submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        # a later bar that breaches the stop 94.0 -> hard-stop exit at the stop.
        bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False, "bearish": True}
        advance_open(s, {("AMD", "1d"): bar}, CFG, today=NEXT)

        closed = _get(s, result.trade_id)
        assert closed.status == "closed"
        assert closed.exit_reason == "stop" and closed.exit_price == 94.0
        assert closed.realized_r == (94.0 - 101.0) / (101.0 - 94.0)  # -1R
        assert load_open_paper_trades(s) == []


def test_stepper_records_research_account_exit_for_research_grid_trade() -> None:
    # the COMPLEMENT to the paper-account case above: a research-grid PaperTrade closing
    # through the SAME stepper records an ExitEvent tagged account="research" -- so the
    # dashboard facet can split the two books that both record under is_paper=True.
    with _session() as s:
        s.add(PaperTrade(
            ticker="AMD", timeframe="1d", horizon="medium", account="research",
            signal_score=0.8, rank=1, fill_status="filled", status="open",
            entry_price=101.0, entry_date=RUN, opened_date=RUN,
            stop=94.0, target=110.0, risk=7.0, high_water=101.0,
        ))
        s.commit()
        bar = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False}
        advance_open(s, {("AMD", "1d"): bar}, CFG, today=NEXT)

        closed = s.query(PaperTrade).filter_by(status="closed").one()
        assert closed.exit_reason == "target"
        ev = s.query(ExitEvent).filter_by(trade_id=closed.id).one()
        assert ev.account == "research"


# ---------------------------------------------------------------------------
# isolation: the paper trade is advanced via load_open_paper_trades but is
# EXCLUDED from the research leaderboard loader (Task-1 account isolation).
# ---------------------------------------------------------------------------
def test_paper_trade_advanced_but_excluded_from_research_leaderboard() -> None:
    with _session() as s:
        PaperAdapter().submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        # it WAS visible to the open-trade stepper loader (account-inclusive)...
        assert len(load_open_paper_trades(s)) == 1

        bar = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False}
        advance_open(s, {("AMD", "1d"): bar}, CFG, today=NEXT)

        # ...but once closed it is NOT in the research-pinned closed loader.
        assert load_closed_paper_trades(s) == []                       # account="research" only
        assert s.query(PaperTrade).filter_by(status="closed").count() == 1  # it really closed


# ---------------------------------------------------------------------------
# idempotency: a duplicate submit must NOT open a second position.
# ---------------------------------------------------------------------------
def test_duplicate_submit_does_not_open_second_position() -> None:
    with _session() as s:
        adapter = PaperAdapter()
        first = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        second = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        assert s.query(PaperTrade).count() == 1       # no double-open
        assert s.query(ExecutionLog).count() == 1     # the unique key collapses the log too
        assert second.status == "filled_paper"
        assert second.trade_id == first.trade_id      # the prior fill is handed back


# ---------------------------------------------------------------------------
# skip-then-fill on the SAME run_date: the freed limit lets the second submit
# open the position, and the log row must be UPGRADED skipped -> filled_paper
# (never swallowed) or the no-double-open guard misses. A THIRD submit --
# UNLIMITED, so only the guard stands between it and a duplicate -- must
# short-circuit without opening a second position.
# ---------------------------------------------------------------------------
def test_skip_then_fill_then_third_submit_short_circuits() -> None:
    with _session() as s:
        # an open paper position AT a max_concurrent cap of 1 -> the FIRST submit is clamped.
        blocker = PaperTrade(ticker="X", timeframe="1d", horizon="medium", account="paper",
                             signal_score=0.5, rank=1, fill_status="filled", status="open",
                             stop=9.0, target=12.0, risk=1.0)
        s.add(blocker)
        s.commit()
        limits = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=1)
        adapter = PaperAdapter()

        first = adapter.submit(_intent(), session=s, run_date=RUN, limits=limits)
        assert first.status == "skipped"

        # the cap frees intra-day (the blocking position closes) -> the re-submit ACTS:
        # one AMD position opens and the single log row upgrades skipped -> filled_paper.
        blocker.status = "closed"
        s.commit()
        second = adapter.submit(_intent(), session=s, run_date=RUN, limits=limits)
        assert second.status == "filled_paper"
        assert s.query(PaperTrade).filter_by(ticker="AMD").count() == 1
        row = s.query(ExecutionLog).one()
        assert row.status == "filled_paper"

        # the THIRD submit (no caps in the way) short-circuits on the counting row:
        # NO duplicate position, the prior fill is handed back.
        third = adapter.submit(_intent(), session=s, run_date=RUN, limits=NO_LIMITS)
        assert third.status == "filled_paper"
        assert third.trade_id == second.trade_id
        assert s.query(PaperTrade).filter_by(ticker="AMD").count() == 1
        assert s.query(ExecutionLog).count() == 1


# ---------------------------------------------------------------------------
# a blocked limit opens nothing (reuse a cap from Task 4).
# ---------------------------------------------------------------------------
def test_blocked_limit_opens_nothing() -> None:
    with _session() as s:
        # an open paper position already AT a max_concurrent cap of 1 -> the next is blocked.
        s.add(PaperTrade(ticker="X", timeframe="1d", horizon="medium", account="paper",
                         signal_score=0.5, rank=1, fill_status="filled", status="open",
                         stop=9.0, target=12.0, risk=1.0))
        s.commit()
        limits = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=1)
        result = PaperAdapter().submit(_intent(), session=s, run_date=RUN, limits=limits)
        assert result.status == "skipped"
        assert "concurrent" in result.detail.lower() or "position" in result.detail.lower()
        # the over-cap intent opened NO new paper trade (only the pre-seeded one remains).
        assert s.query(PaperTrade).count() == 1
        skipped = s.query(ExecutionLog).filter_by(status="skipped").one()
        assert skipped.ticker == "AMD" and skipped.account == "paper"


# ---------------------------------------------------------------------------
# non-positive risk (limit_price <= stop) -> rejected, opens nothing.
# ---------------------------------------------------------------------------
def test_nonpositive_risk_is_rejected_and_opens_nothing() -> None:
    with _session() as s:
        # limit_price 94.0 == stop 94.0 -> risk 0.0, not honestly tradeable.
        result = PaperAdapter().submit(
            _intent(limit_price=94.0), session=s, run_date=RUN, limits=NO_LIMITS)
        assert result.status == "rejected"
        assert s.query(PaperTrade).count() == 0           # opens nothing
        rejected = s.query(ExecutionLog).filter_by(status="rejected").one()
        assert rejected.account == "paper"


def test_adapter_name() -> None:
    assert PaperAdapter().name == "paper"

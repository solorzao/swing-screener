"""Journal v2 read-model producers for the personal books (manual_equity, robinhood)
and the boundary-isolation guarantee (design SS Boundary)."""

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade, PaperTrade, Trade
from swing_screener.db.session import get_engine
from swing_screener.journal.record import (
    TagView,
    manual_equity_records,
    robinhood_records,
    trade_records,
)
from swing_screener.journal.repo import add_tag, tag_trade


def _trade(*, ticker="AMD", entry=100.0, stop=95.0, target=110.0,
           exit_price=None, entry_date=date(2026, 7, 1), exit_date=None):
    return Trade(
        ticker=ticker, timeframe="1d", horizon="medium", entry_date=entry_date,
        entry_price=entry, size=1.0, stop=stop, target=target,
        status="closed" if exit_price is not None else "open",
        exit_price=exit_price, exit_date=exit_date,
        exit_reason="target" if exit_price is not None else None,
    )


def _robin(*, underlying="SPY", pnl=42.0, opened=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
           closed=datetime(2026, 7, 1, 15, 0, tzinfo=UTC), account="robinhood"):
    return OptionPaperTrade(
        account=account, strategy="gex", underlying=underlying, direction="long",
        opened_at=opened, closed_at=closed, premium_pnl=pnl, status="closed",
    )


def test_manual_equity_records_compute_r_and_join_tags():
    with Session(get_engine("sqlite:///:memory:")) as s:
        won = _trade(ticker="AMD", entry=100.0, stop=95.0, exit_price=110.0,
                     exit_date=date(2026, 7, 5))       # (110-100)/(100-95) = 2.0R
        open_ = _trade(ticker="NVDA", exit_price=None)  # still open -> result None
        s.add_all([won, open_])
        s.commit()
        chased = add_tag(s, kind="mistake", name="chased", description="x")
        tag_trade(s, trade_id=won.id, book="manual_equity", tag_id=chased.id, source="human")

        recs = {r.symbol: r for r in manual_equity_records(s)}
        assert recs["AMD"].book == "manual_equity"
        assert recs["AMD"].unit == "R"
        assert recs["AMD"].result == 2.0
        assert recs["AMD"].opened == date(2026, 7, 1)
        assert recs["AMD"].closed == date(2026, 7, 5)
        assert recs["AMD"].tags == [TagView(name="chased", kind="mistake", source="human")]
        assert recs["NVDA"].result is None


def test_manual_equity_result_none_when_risk_nonpositive():
    with Session(get_engine("sqlite:///:memory:")) as s:
        # stop above entry -> risk <= 0 -> result None (never a bogus R)
        bad = _trade(ticker="X", entry=100.0, stop=100.0, exit_price=105.0,
                     exit_date=date(2026, 7, 2))
        s.add(bad)
        s.commit()
        assert manual_equity_records(s)[0].result is None


def test_robinhood_records_dollar_unit_and_date_coercion():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(_robin(underlying="SPY", pnl=42.0))
        s.add(_robin(underlying="QQQ", pnl=-13.0, account="options-lab"))  # must NOT appear
        s.commit()
        recs = robinhood_records(s)
        assert [r.symbol for r in recs] == ["SPY"]     # options-lab excluded
        assert recs[0].unit == "$"
        assert recs[0].module == "gex"
        assert recs[0].result == 42.0
        assert recs[0].opened == date(2026, 7, 1)      # datetime -> date coerced


def test_zero_leak_between_personal_and_machine_producers():
    """The design SS Boundary guarantee, executable: personal producers return no
    machine rows, and the machine producer returns no personal rows."""
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(_trade(ticker="AMD", exit_price=110.0, exit_date=date(2026, 7, 5)))
        s.add(_robin(underlying="SPY"))
        s.add(_robin(underlying="QQQ", account="options-lab"))
        s.add(PaperTrade(ticker="MSFT", timeframe="1d", horizon="medium",
                         signal_score=0.8, rank=1, account="research",
                         fill_status="filled", stop=95.0, target=110.0, risk=5.0,
                         status="closed", realized_r=1.0))
        s.add(PaperTrade(ticker="TSLA", timeframe="1d", horizon="medium",
                         signal_score=0.8, rank=1, account="live",
                         fill_status="filled", stop=95.0, target=110.0, risk=5.0,
                         status="closed", realized_r=1.0))
        s.commit()

        manual_syms = {r.symbol for r in manual_equity_records(s)}
        robin_syms = {r.symbol for r in robinhood_records(s)}
        machine_syms = {r.symbol for r in trade_records(s, book="research")}

        assert manual_syms == {"AMD"}               # only the Trade row
        assert robin_syms == {"SPY"}                # only the robinhood option
        assert machine_syms == {"MSFT"}             # only the research PaperTrade
        # no cross-contamination
        assert not (manual_syms | robin_syms) & machine_syms

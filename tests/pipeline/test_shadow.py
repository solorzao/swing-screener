from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import repo
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.shadow import FillCandidate, advance_open, open_from_signals
from swing_screener.signals.entry_zone import EntryZone

CFG = StrategyConfig()
ZONE = EntryZone(floor=96.0, ceiling=101.0, stop=94.0, target=110.0, risk=4.0, reference=98.5)


def _cand(ticker="AAPL"):
    return FillCandidate(ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8,
                         rank=1, mtf_aligned=True, signal_id=None, zone=ZONE)


def _all_paper_trades(session):
    return list(session.scalars(select(PaperTrade)))


def test_nonpositive_risk_fill_is_downgraded_to_invalidated():
    # a degenerate zone whose worst-case fill sits at/below the stop yields
    # risk <= 0; it must be downgraded to invalidated (never opened), so the
    # later realized_r division can never hit a zero divisor.
    engine = get_engine("sqlite:///:memory:")
    degenerate = EntryZone(floor=100.0, ceiling=101.0, stop=101.0, target=110.0,
                           risk=0.0, reference=100.5)
    cand = FillCandidate(ticker="AAPL", timeframe="1d", horizon="medium", signal_score=0.8,
                         rank=1, mtf_aligned=True, signal_id=None, zone=degenerate)
    with Session(engine) as s:
        open_from_signals(s, [cand], {("AAPL", "1d"): (100.5, 100.0)}, fill_date=date(2024, 1, 3))
        assert repo.load_open_paper_trades(s) == []
        pt = _all_paper_trades(s)[0]
        assert pt.fill_status == "invalidated" and pt.entry_price is None


def test_missed_when_next_bar_gaps_above_ceiling():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_cand()], {("AAPL", "1d"): (120.0, 102.0)}, fill_date=date(2024, 1, 3))
        assert repo.load_open_paper_trades(s) == []  # missed -> not open

        rows = _all_paper_trades(s)
        assert len(rows) == 1
        pt = rows[0]
        assert pt.fill_status == "missed"
        assert pt.status == "closed"
        assert pt.entry_price is None


def test_fill_then_hard_stop_closes_with_negative_r():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        # next bar trades through the zone -> worst-case fill at ceiling (101.0)
        open_from_signals(s, [_cand()], {("AAPL", "1d"): (105.0, 97.0)}, fill_date=date(2024, 1, 3))
        opened = repo.load_open_paper_trades(s)
        assert len(opened) == 1
        pt = opened[0]
        assert pt.entry_price == 101.0 and pt.hold_bars == 0
        assert pt.risk == 101.0 - 94.0  # entry - stop

        # a bar that breaches the stop -> hard-stop exit
        bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False, "bearish": True}
        advance_open(s, {("AAPL", "1d"): bar}, CFG, today=date(2024, 1, 4))
        closed = s.get(type(pt), pt.id)
        assert closed.status == "closed" and closed.exit_reason == "stop"
        assert closed.exit_price == 94.0
        assert closed.realized_r < 0           # ~ -1R
        assert repo.load_open_paper_trades(s) == []


def test_hold_increments_bars_held():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_cand()], {("AAPL", "1d"): (105.0, 97.0)}, fill_date=date(2024, 1, 3))
        quiet = {"low": 99.0, "high": 103.0, "close": 100.0, "shaved_head": False, "bearish": False}
        advance_open(s, {("AAPL", "1d"): quiet}, CFG, today=date(2024, 1, 4))
        pt = repo.load_open_paper_trades(s)[0]
        assert pt.hold_bars == 1 and pt.status == "open"

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import PaperTrade, Signal
from swing_screener.db.session import get_engine


def _signal(ticker="AAPL", rank=1, score=0.8):
    return Signal(
        run_date=date(2024, 1, 2), ticker=ticker, timeframe="1d", horizon="medium",
        score=score, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
        entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
    )


def test_save_and_latest_signals_ordered_by_rank():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_signals(s, [_signal("MSFT", rank=2, score=0.6), _signal("AAPL", rank=1, score=0.9)])
        got = repo.latest_signals(s, date(2024, 1, 2))
        assert [x.ticker for x in got] == ["AAPL", "MSFT"]  # ascending rank


def test_open_paper_trades_and_record_exit():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [PaperTrade(
            ticker="AAPL", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
            fill_status="filled", stop=95.0, target=110.0, risk=5.0, status="open",
        )])
        opened = repo.load_open_paper_trades(s)
        assert len(opened) == 1 and opened[0].ticker == "AAPL"

        ev = repo.record_exit_event(s, is_paper=True, trade_id=opened[0].id,
                                    tier="hard", reason="stop", message="stopped out",
                                    created_date=date(2024, 1, 5))
        assert ev.id is not None and ev.reason == "stop"


def test_list_universe_orders_and_filters_by_ticker():
    from sqlalchemy.orm import Session
    from swing_screener.db.models import Universe
    from swing_screener.db.session import get_engine
    from swing_screener.db import repo
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([Universe(ticker="NVDA", name="Nvidia"),
                   Universe(ticker="AMD", name="Advanced Micro")])
        s.commit()
        assert [u.ticker for u in repo.list_universe(s)] == ["AMD", "NVDA"]   # ordered by ticker
        assert [u.ticker for u in repo.list_universe(s, search="nv")] == ["NVDA"]  # case-insensitive

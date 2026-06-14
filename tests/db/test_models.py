from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db.models import PaperTrade, Signal
from swing_screener.db.session import get_engine


def test_signal_and_paper_trade_roundtrip():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(Signal(
            run_date=date(2024, 1, 2), ticker="AAPL", timeframe="1d", horizon="medium",
            score=0.81, rank=1, mtf_aligned=True, quality_tier="reputable",
            volatility_tier="med", oversold=False, trigger_close=100.0, atr=4.0, rsi=55.0,
            entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
        ))
        s.add(PaperTrade(
            ticker="AAPL", timeframe="1d", horizon="medium", signal_score=0.81, rank=1,
            mtf_aligned=True, fill_status="filled", entry_date=date(2024, 1, 3),
            entry_price=100.5, stop=95.0, target=110.0, risk=5.5, status="open",
        ))
        s.commit()

        sig = s.query(Signal).filter_by(ticker="AAPL").one()
        assert sig.score == 0.81 and sig.rank == 1 and sig.mtf_aligned is True
        pt = s.query(PaperTrade).filter_by(ticker="AAPL").one()
        assert pt.fill_status == "filled" and pt.risk == 5.5
        assert pt.exit_reason is None  # nullable defaults work

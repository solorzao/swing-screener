from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db.models import ExitEvent, Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import select as sel

RUN = date(2026, 6, 15)


def _sig(ticker, tf, rank, first_seen=None):
    return Signal(run_date=RUN, ticker=ticker, timeframe=tf, horizon="medium", score=1.0 / rank,
                  rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0, entry_floor=96.0,
                  entry_ceiling=101.0, stop=95.0, target=110.0, first_seen_date=first_seen)


def _seed():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        # ranks are globally unique across timeframes (as the orchestrator assigns
        # them); the 1wk signal MSFT outranks some 1d signals.
        s.add_all([
            _sig("AMD", "1d", 1), _sig("AEP", "1d", 2), _sig("MSFT", "1wk", 3),
            _sig("A", "1d", 4), _sig("AAPL", "1d", 5), _sig("ABBV", "1d", 6),
            _sig("NVDA", "1mo", 7),
        ])
        s.add_all([
            ExitEvent(created_date=RUN, is_paper=False, trade_id=10, tier="hard",
                      reason="stop", message="stopped"),
            ExitEvent(created_date=RUN, is_paper=True, trade_id=99, tier="strong",
                      reason="momentum_flip", message="paper"),
        ])
        s.commit()
        return s, engine  # keep session open for the test


def test_daily_top_n_is_overall_rank_across_timeframes():
    s, _ = _seed()
    picks = sel.daily_picks(s, RUN, top_n=5)
    # top 5 by global rank — includes the rank-3 weekly signal (MSFT), capped at 5
    assert [p.ticker for p in picks] == ["AMD", "AEP", "MSFT", "A", "AAPL"]


def test_weekly_and_monthly_filter_by_timeframe():
    s, _ = _seed()
    assert [p.ticker for p in sel.weekly_picks(s, RUN)] == ["MSFT"]
    assert [p.ticker for p in sel.monthly_picks(s, RUN)] == ["NVDA"]


def test_exit_alerts_only_real_trades():
    s, _ = _seed()
    alerts = sel.pending_exit_alerts(s, RUN)
    assert len(alerts) == 1 and alerts[0].reason == "stop" and alerts[0].is_paper is False


def _rev(ticker, rank, strength="early", first_seen=None):
    return Signal(run_date=RUN, ticker=ticker, timeframe="1d", horizon="medium",
                  play_type="reversal", strength=strength, score=1.0 / rank, rank=rank,
                  trigger_close=50.0, atr=2.0, rsi=22.0, entry_floor=50.0, entry_ceiling=52.0,
                  stop=47.0, target=58.0, first_seen_date=first_seen)


def test_cooldown_drops_stale_repeats_keeps_fresh_and_legacy():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([
            _sig("AMD", "1d", 1, first_seen=RUN),             # first seen today
            _sig("AEP", "1d", 2, first_seen=date(2026, 6, 1)),  # old streak (stale)
            _sig("MSFT", "1d", 3),                            # first_seen None (legacy)
        ])
        s.commit()

        # cooldown of 1 day -> AEP (first seen 14 days ago) drops; today's + legacy stay.
        picks = sel.daily_picks(s, RUN, max_age_days=1)
        assert [p.ticker for p in picks] == ["AMD", "MSFT"]
        # no cooldown -> all three, by rank
        assert [p.ticker for p in sel.daily_picks(s, RUN)] == ["AMD", "AEP", "MSFT"]


def test_cooldown_applies_to_reversal_picks():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([_rev("GME", 1, first_seen=RUN),
                   _rev("BBBY", 2, first_seen=date(2026, 6, 1))])
        s.commit()
        assert [p.ticker for p in sel.reversal_picks(s, RUN, max_age_days=1)] == ["GME"]


def test_reversal_picks_are_separate_from_continuation():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([_sig("AMD", "1d", 1), _sig("AEP", "1d", 2)])           # continuation
        s.add_all([_rev("GME", 1, "confirmed"), _rev("BBBY", 2, "early")])  # reversal
        s.commit()

        cont = sel.daily_picks(s, RUN)
        rev = sel.reversal_picks(s, RUN)
        assert [p.ticker for p in cont] == ["AMD", "AEP"]   # reversals excluded
        assert [p.ticker for p in rev] == ["GME", "BBBY"]   # only reversals, by rank
        assert rev[0].strength == "confirmed" and rev[0].play_type == "reversal"

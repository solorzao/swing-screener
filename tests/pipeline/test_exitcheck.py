from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import ExitEvent, Trade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.exitcheck import run_exit_check

RUN = date(2026, 6, 15)


def _trade(ticker: str, *, stop: float = 95.0, target: float = 110.0) -> Trade:
    # entry a few days before the run so the bars-held analog is non-zero.
    return Trade(ticker=ticker, timeframe="1d", horizon="medium", entry_date=date(2026, 6, 10),
                 entry_price=100.0, size=10.0, stop=stop, target=target, status="open")


def _seed_two_open(url: str) -> None:
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_trade("AMD"), _trade("AEP")])
        s.commit()


def _exit_events(url: str) -> list[ExitEvent]:
    with Session(get_engine(url)) as s:
        stmt = select(ExitEvent).where(ExitEvent.is_paper.is_(False)).order_by(ExitEvent.id)
        return list(s.scalars(stmt))


# one bar trips a hard stop (low <= stop), the other holds.
_BARS = {
    "AMD": {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False, "bearish": True},
    "AEP": {"low": 99.0, "high": 103.0, "close": 100.0, "shaved_head": False, "bearish": False},
}


def _fake_bars(tickers, timeframe):  # noqa: ARG001 - mirror the live seam signature
    return {t: _BARS[t] for t in tickers if t in _BARS}


def test_exit_check_writes_one_real_exit_event(tmp_path):
    url = f"sqlite:///{tmp_path / 'x.sqlite'}"
    _seed_two_open(url)

    result = run_exit_check(db_url=url, today=RUN, latest_bars_fn=_fake_bars)

    assert result.n_open == 2
    assert result.n_exited == 1

    events = _exit_events(url)
    assert len(events) == 1
    ev = events[0]
    assert ev.reason == "stop" and ev.tier == "hard"
    assert ev.created_date == RUN
    assert "AMD" in ev.message  # holder (AEP) wrote nothing


def test_exit_check_is_idempotent_within_the_day(tmp_path):
    url = f"sqlite:///{tmp_path / 'i.sqlite'}"
    _seed_two_open(url)

    run_exit_check(db_url=url, today=RUN, latest_bars_fn=_fake_bars)
    again = run_exit_check(db_url=url, today=RUN, latest_bars_fn=_fake_bars)

    # the hourly re-run must not pile up duplicates.
    assert again.n_open == 2 and again.n_exited == 0
    assert len(_exit_events(url)) == 1


def test_exit_check_does_not_mutate_real_trades(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    _seed_two_open(url)

    run_exit_check(db_url=url, today=RUN, latest_bars_fn=_fake_bars)

    with Session(get_engine(url)) as s:
        for t in s.scalars(select(Trade)):
            # this path only ALERTS; a human closes trades in the dashboard.
            assert t.status == "open"
            assert t.exit_date is None and t.exit_price is None and t.exit_reason is None


def test_reversal_trade_uses_the_reversal_exit_policy(tmp_path):
    """A real REVERSAL trade must not be alerted to momentum-flip-exit: the replay
    evidence says the flip converts +0.77R reversal outcomes into ~-0.19R early cuts
    (reversal_momentum_flip_exit defaults off). Before play_type was resolved through
    the signal_id FK, every real trade defaulted to 'continuation' and reversal
    positions received the continuation flip-exit advice."""
    from swing_screener.db.models import Signal

    url = f"sqlite:///{tmp_path / 'rev.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        rev_sig = Signal(run_date=RUN, ticker="GME", timeframe="1d", horizon="medium",
                         play_type="reversal", strength="confirmed", score=0.5, rank=1,
                         trigger_close=100.0, atr=2.0, rsi=30.0, entry_floor=96.0,
                         entry_ceiling=101.0, stop=95.0, target=110.0)
        cont_sig = Signal(run_date=RUN, ticker="AMD", timeframe="1d", horizon="medium",
                          play_type="continuation", score=0.5, rank=2,
                          trigger_close=100.0, atr=2.0, rsi=55.0, entry_floor=96.0,
                          entry_ceiling=101.0, stop=95.0, target=110.0)
        s.add_all([rev_sig, cont_sig])
        s.flush()
        rev = _trade("GME")
        rev.signal_id = rev_sig.id
        cont = _trade("AMD")
        cont.signal_id = cont_sig.id
        legacy = _trade("AEP")  # no signal_id: legacy/manual -> continuation fallback
        s.add_all([rev, cont, legacy])
        s.commit()

    # a bearish shaved-head bar: the momentum-flip trigger (no stop/target/time trip)
    flip = {"low": 99.0, "high": 103.0, "close": 100.0, "shaved_head": True, "bearish": True}

    def flip_bars(tickers, timeframe):  # noqa: ARG001 - mirror the live seam signature
        return {t: flip for t in tickers}

    result = run_exit_check(db_url=url, today=RUN, latest_bars_fn=flip_bars)

    events = _exit_events(url)
    assert result.n_exited == 2
    exited = {e.message.split()[0] for e in events}
    assert exited == {"AMD", "AEP"}          # continuation + legacy fallback flip out
    assert all(e.reason == "momentum_flip" for e in events)
    assert "GME" not in exited               # the reversal position rides (policy: no flip)


# benign bar: low > stop, high < target, no shaved head -> only the time-stop tier
# can fire, so this isolates the bars_held calculation.
_BENIGN = {"low": 99.0, "high": 103.0, "close": 100.0, "shaved_head": False, "bearish": False}


def _weekly_trade(ticker: str, entry: date) -> Trade:
    return Trade(ticker=ticker, timeframe="1wk", horizon="long", entry_date=entry,
                 entry_price=100.0, size=10.0, stop=95.0, target=110.0, status="open")


def test_time_stop_counts_timeframe_bars_not_calendar_days(tmp_path):
    # max_hold_bars["1wk"] == 8 (weeks). A weekly trade 60 days old is 8 weekly
    # bars -> time-stop fires; one 21 days old is 3 weekly bars -> holds. Under a
    # raw calendar-day count the 21-day holder would be 21 >= 8 and wrongly exit,
    # so this pins the per-timeframe bar conversion.
    url = f"sqlite:///{tmp_path / 'ts.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([
            _weekly_trade("OLD", date(2026, 4, 16)),    # 60 days -> 8 weekly bars
            _weekly_trade("YOUNG", date(2026, 5, 25)),  # 21 days -> 3 weekly bars
        ])
        s.commit()

    def benign_bars(tickers, timeframe):  # noqa: ARG001 - mirror the live seam signature
        return {t: _BENIGN for t in tickers}

    result = run_exit_check(db_url=url, today=RUN, latest_bars_fn=benign_bars)

    assert result.n_exited == 1
    events = _exit_events(url)
    assert len(events) == 1
    assert events[0].reason == "time_stop" and events[0].tier == "advisory"
    assert "OLD" in events[0].message  # the 3-week-old YOUNG holds

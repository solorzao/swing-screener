from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import ExitEvent, PaperTrade, Trade
from swing_screener.db.session import get_engine


def _trade(ticker="AAPL"):
    return Trade(ticker=ticker, timeframe="1d", horizon="medium", entry_date=date(2024, 1, 2),
                 entry_price=100.0, size=10.0, stop=95.0, target=110.0)


def _paper_trade(account: str, opened: date) -> PaperTrade:
    return PaperTrade(ticker="AMD", timeframe="1d", horizon="medium", signal_score=0.8,
                      rank=1, account=account, fill_status="filled", stop=95.0,
                      target=110.0, risk=5.0, status="open", opened_date=opened)


def test_add_and_list_open_trades():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        t = repo.add_trade(s, _trade())
        assert t.id is not None and t.status == "open"
        assert [x.ticker for x in repo.get_open_trades(s)] == ["AAPL"]
        assert repo.get_closed_trades(s) == []


def test_close_trade_moves_to_closed():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        t = repo.add_trade(s, _trade())
        closed = repo.close_trade(s, t.id, exit_date=date(2024, 1, 9),
                                  exit_price=108.0, exit_reason="target")
        assert closed.status == "closed" and closed.exit_price == 108.0
        assert repo.get_open_trades(s) == []
        assert [x.ticker for x in repo.get_closed_trades(s)] == ["AAPL"]


def test_close_trade_already_closed_raises_distinct_error():
    # Re-closing would silently overwrite the recorded exit; the guard raises
    # AlreadyClosedError -- a ValueError SUBCLASS, so callers catching ValueError
    # keep working, while the cockpit endpoint maps it to 409 (unknown id -> 404).
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        t = repo.add_trade(s, _trade())
        repo.close_trade(s, t.id, exit_date=date(2024, 1, 9),
                         exit_price=108.0, exit_reason="target")
        with pytest.raises(repo.AlreadyClosedError):
            repo.close_trade(s, t.id, exit_date=date(2024, 1, 10),
                             exit_price=109.0, exit_reason="manual")
        closed = s.get(Trade, t.id)
        assert closed is not None and closed.exit_price == 108.0  # first exit survives
        assert issubclass(repo.AlreadyClosedError, ValueError)
        # the unknown-id flavor stays a PLAIN ValueError, never the subclass
        with pytest.raises(ValueError) as err:
            repo.close_trade(s, 999, exit_date=date(2024, 1, 9),
                             exit_price=1.0, exit_reason="manual")
        assert not isinstance(err.value, repo.AlreadyClosedError)


def test_close_trade_with_event_carries_both_rows():
    # the cockpit's manual close: the trade UPDATE and its ExitEvent ride ONE
    # commit (all-or-nothing -- see the function docstring), and the guard raises
    # exactly like close_trade.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        t = repo.add_trade(s, _trade())
        closed, event = repo.close_trade_with_event(
            s, t.id, exit_date=date(2024, 1, 9), exit_price=108.0,
            exit_reason="target", event_reason="manual_close",
            event_message="AAPL closed manually @ 108", created_date=date(2024, 1, 9))
        assert closed.status == "closed" and closed.exit_price == 108.0
        assert event.id is not None and event.trade_id == t.id
        assert event.reason == "manual_close" and event.is_paper is False
        assert event.tier == "" and event.account == "research"
        with pytest.raises(repo.AlreadyClosedError):
            repo.close_trade_with_event(
                s, t.id, exit_date=date(2024, 1, 10), exit_price=1.0,
                exit_reason="x", event_reason="manual_close", event_message="",
                created_date=date(2024, 1, 10))


def test_concurrent_close_cannot_overwrite_the_recorded_exit(tmp_path):
    """The lost-update window: a session that read the trade OPEN (a stale
    identity map -- exactly what two concurrent cockpit requests hold on Azure
    SQL) must NOT overwrite an exit another session recorded in between. The
    close is an atomic ``UPDATE ... WHERE status='open'``: the loser's update
    matches zero rows and raises AlreadyClosedError (the endpoint's 409), the
    first close's exit fields and its single ExitEvent stand."""
    engine = get_engine(f"sqlite:///{(tmp_path / 'race.db').as_posix()}")
    with Session(engine) as s:
        tid = repo.add_trade(s, _trade()).id
    with Session(engine) as s1, Session(engine) as s2:
        # s1's stale read -- the strong reference matters: the endpoint holds one
        # (its `pre` local), and without it the weak identity map would GC the
        # instance and quietly re-select, hiding the very window under test.
        stale = s1.get(Trade, tid)
        assert stale is not None and stale.status == "open"
        repo.close_trade_with_event(
            s2, tid, exit_date=date(2024, 1, 9), exit_price=108.0,
            exit_reason="target", event_reason="manual_close",
            event_message="first close", created_date=date(2024, 1, 9))
        with pytest.raises(repo.AlreadyClosedError):
            repo.close_trade_with_event(
                s1, tid, exit_date=date(2024, 1, 10), exit_price=1.0,
                exit_reason="x", event_reason="manual_close",
                event_message="second close", created_date=date(2024, 1, 10))
    with Session(engine) as s:
        t = s.get(Trade, tid)
        assert t.exit_price == 108.0 and t.exit_date == date(2024, 1, 9)
        assert t.exit_reason == "target"                # the first close stands
        events = list(s.scalars(select(ExitEvent)))
        assert [e.message for e in events] == ["first close"]  # exactly ONE event


def test_delete_paper_trades_opened_on_only_touches_research():
    """A same-day re-run delete must never touch live/paper/manual rows: only the
    research shadow grid is re-created by the re-run, so deleting any other book
    permanently loses real positions (2026-07-17 audit, critical)."""
    engine = get_engine("sqlite:///:memory:")
    opened = date(2026, 7, 17)
    with Session(engine) as s:
        s.add_all([_paper_trade(a, opened)
                   for a in ("research", "live", "paper", "manual")])
        s.commit()
        repo.delete_paper_trades_opened_on(s, opened)
        assert set(s.scalars(select(PaperTrade.account))) == {"live", "paper", "manual"}


def test_update_trade_patches_fields():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        t = repo.add_trade(s, _trade())
        updated = repo.update_trade(s, t.id, stop=97.0, notes="raised stop")
        assert updated.stop == 97.0 and updated.notes == "raised stop"

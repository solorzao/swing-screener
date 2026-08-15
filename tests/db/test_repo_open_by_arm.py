"""``count_open_by_arm``: the drain-completion probe.

A retired arm stays in ``build_draining_arms`` only while it still has open rows. This
count is what tells the screen when that is no longer true, so it must count OPEN rows
only, across every account, and stay silent about arms it wasn't asked for.
"""

from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine


def _pt(*, ticker="AAPL", arm="baseline", status="open", account="research"):
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        play_type="continuation", arm=arm, variant="default", account=account,
        fill_status="filled", stop=95.0, target=110.0, risk=5.0, status=status,
        realized_r=None if status == "open" else 1.0,
    )


def test_counts_only_open_rows_for_the_named_arms():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [
            _pt(ticker="A", arm="no_flip"),
            _pt(ticker="B", arm="no_flip"),
            _pt(ticker="C", arm="no_flip", status="closed"),   # closed -> not counted
            _pt(ticker="D", arm="be_1r"),
            _pt(ticker="E", arm="baseline"),                   # not asked for -> absent
        ])
        got = repo.count_open_by_arm(s, ["no_flip", "be_1r", "partial33_cond"])
        assert got == {"no_flip": 2, "be_1r": 1}   # fully drained arm is simply absent


def test_counts_across_accounts_so_a_straggler_cannot_hide():
    # A draining arm is "finished" only when NOTHING is open under it anywhere; scoping
    # to the research grid would let a row on another book keep it silently alive.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [
            _pt(ticker="A", arm="no_flip", account="research"),
            _pt(ticker="B", arm="no_flip", account="paper"),
        ])
        assert repo.count_open_by_arm(s, ["no_flip"]) == {"no_flip": 2}


def test_empty_arm_list_short_circuits():
    # An empty IN () is a SQL Server syntax error, so the no-draining-arms case must
    # never reach the database.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        assert repo.count_open_by_arm(s, []) == {}

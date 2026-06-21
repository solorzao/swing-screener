from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine


def _pt(*, ticker="AAPL", play_type="continuation", arm="baseline", variant="default",
        status="closed", fill_status="filled", realized_r=1.0):
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        play_type=play_type, arm=arm, variant=variant,
        fill_status=fill_status, stop=95.0, target=110.0, risk=5.0, status=status,
        realized_r=realized_r,
    )


def test_load_closed_returns_only_filled_closed_with_realized():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [
            _pt(ticker="WIN"),                                      # closed+filled+realized -> in
            _pt(ticker="OPEN", status="open", realized_r=None),    # open -> out
            _pt(ticker="MISS", fill_status="missed", realized_r=None),  # missed -> out
            _pt(ticker="NOR", realized_r=None),                    # closed+filled, no R -> out
        ])
        got = repo.load_closed_paper_trades(s)
        assert [t.ticker for t in got] == ["WIN"]


def test_load_closed_facets_filter_independently():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [
            _pt(ticker="A", play_type="continuation", arm="baseline", variant="default"),
            _pt(ticker="B", play_type="reversal", arm="baseline", variant="default"),
            _pt(ticker="C", play_type="continuation", arm="partial33_cond", variant="default"),
            _pt(ticker="D", play_type="continuation", arm="baseline", variant="tight"),
        ])
        # each facet excludes the wrong-valued closed+filled rows when set
        assert {t.ticker for t in repo.load_closed_paper_trades(s, play_type="continuation")} \
            == {"A", "C", "D"}
        assert {t.ticker for t in repo.load_closed_paper_trades(s, arm="baseline")} \
            == {"A", "B", "D"}
        assert {t.ticker for t in repo.load_closed_paper_trades(s, variant="default")} \
            == {"A", "B", "C"}
        # the live-forward-book slice: all three facets together
        assert [t.ticker for t in repo.load_closed_paper_trades(
            s, play_type="continuation", arm="baseline", variant="default")] == ["A"]


def test_load_closed_facets_still_exclude_open_and_missed():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [
            _pt(ticker="A", play_type="reversal"),                                 # in
            _pt(ticker="OPEN", play_type="reversal", status="open", realized_r=None),
            _pt(ticker="MISS", play_type="reversal", fill_status="missed", realized_r=None),
        ])
        # even with a matching facet, open/missed rows never come back
        assert [t.ticker for t in repo.load_closed_paper_trades(s, play_type="reversal")] == ["A"]

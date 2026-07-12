"""Task 9 test contract: the unified TradeRecord read-model (swing source).

``trade_records(session, *, book)`` is a DISPLAY-ONLY normalization over PaperTrade
for one swing book -- it never aggregates (statistics stay in the analytics fns). Each
record is R-native (unit="R"), module="swing", long-convention, and carries its applied
tags and theses (with provenance) resolved to display views. A different book's rows
never leak in. Designed so a future GEX OptionPaperTrade folds in as module="gex"
without changing consumers.
"""

from datetime import date, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.journal.record import TagView, ThesisView, TradeRecord, trade_records
from swing_screener.journal.repo import add_tag, add_thesis, tag_trade


def _pt(*, r=None, status="closed", account="research", ticker="AMD",
        opened=None, closed=None):
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account=account, fill_status="filled", stop=95.0, target=110.0, risk=5.0,
        status=status, realized_r=r, opened_date=opened, exit_date=closed,
    )


def test_trade_records_shape_tags_and_theses():
    with Session(get_engine("sqlite:///:memory:")) as s:
        closed = _pt(r=2.0, opened=date(2026, 7, 1), closed=date(2026, 7, 5), ticker="AMD")
        open_ = _pt(r=None, status="open", opened=date(2026, 7, 6), ticker="NVDA")
        other_book = _pt(r=-1.0, account="paper", ticker="MSFT")
        s.add_all([closed, open_, other_book])
        s.commit()

        chased = add_tag(s, kind="mistake", name="chased", description="too extended")
        tag_trade(s, trade_id=closed.id, book="research", tag_id=chased.id, source="human")
        add_thesis(s, trade_id=closed.id, book="research", event_kind="entry",
                   source="screener", body="pullback into 20EMA",
                   created_at=datetime(2026, 7, 1, 9, 30))
        # a thesis on the SAME trade id but a different book must not attach here
        add_thesis(s, trade_id=closed.id, book="paper", event_kind="entry",
                   source="human", body="wrong book")

        records = trade_records(s, book="research")
        assert all(isinstance(r, TradeRecord) for r in records)
        # only the two research rows, the paper row excluded
        assert {r.symbol for r in records} == {"AMD", "NVDA"}

        by_symbol = {r.symbol: r for r in records}
        amd = by_symbol["AMD"]
        assert amd.book == "research"
        assert amd.module == "swing"
        assert amd.unit == "R"
        assert amd.direction == "long"
        assert amd.opened == date(2026, 7, 1)
        assert amd.closed == date(2026, 7, 5)
        assert amd.r == 2.0
        assert amd.tags == [TagView(name="chased", kind="mistake", source="human")]
        assert amd.theses == [
            ThesisView(event_kind="entry", source="screener", body="pullback into 20EMA")
        ]

        nvda = by_symbol["NVDA"]
        assert nvda.closed is None
        assert nvda.r is None
        assert nvda.tags == []
        assert nvda.theses == []


def test_trade_records_empty_book_is_empty():
    with Session(get_engine("sqlite:///:memory:")) as s:
        assert trade_records(s, book="research") == []

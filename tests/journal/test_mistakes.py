"""Task 8 test contract: the mistake-cost report.

``mistake_cost(session, trades)`` joins human/analyst MISTAKE tags to their trades,
groups by mistake name, and reports the realized cost (sum R) + count with an honest
``summarize`` per group. Only ``kind="mistake"`` tags count, only ``human``/``analyst``
provenance (a screener-applied tag is not a confessed mistake), only closed-filled
trades (an open trade has no realized cost yet), and a trade tagged the same mistake
by two sources counts ONCE. Ordered worst-first (most negative total R). Empty/None-safe.
"""

from swing_screener.analytics.performance import PerformanceSummary, summarize
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.journal.mistakes import mistake_cost
from swing_screener.journal.repo import add_tag, tag_trade
from sqlalchemy.orm import Session


def _pt(r, *, status="closed", fill_status="filled", account="research"):
    return PaperTrade(
        ticker="AMD", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account=account, fill_status=fill_status, stop=95.0, target=110.0, risk=5.0,
        status=status, realized_r=r,
    )


def test_mistake_cost_groups_sums_and_orders_worst_first():
    with Session(get_engine("sqlite:///:memory:")) as s:
        t1, t2, t3, t4 = _pt(-2.0), _pt(-2.2), _pt(1.0), _pt(0.5)
        s.add_all([t1, t2, t3, t4])
        s.commit()

        chased = add_tag(s, kind="mistake", name="chased")
        moved = add_tag(s, kind="mistake", name="moved_stop")
        setup = add_tag(s, kind="setup", name="pullback")

        tag_trade(s, trade_id=t1.id, book="research", tag_id=chased.id, source="human")
        tag_trade(s, trade_id=t2.id, book="research", tag_id=chased.id, source="human")
        tag_trade(s, trade_id=t3.id, book="research", tag_id=moved.id, source="analyst")
        # a setup tag is not a mistake; a screener-sourced mistake tag is not a confession
        tag_trade(s, trade_id=t4.id, book="research", tag_id=setup.id, source="human")
        tag_trade(s, trade_id=t4.id, book="research", tag_id=chased.id, source="screener")

        rows = mistake_cost(s, [t1, t2, t3, t4])
        assert [r["mistake"] for r in rows] == ["chased", "moved_stop"]  # worst-first
        chased_row = rows[0]
        assert chased_row["n"] == 2
        assert abs(chased_row["total_r"] - (-4.2)) < 1e-9
        assert isinstance(chased_row["summary"], PerformanceSummary)
        assert chased_row["summary"] == summarize([t1, t2])
        assert rows[1]["n"] == 1
        assert abs(rows[1]["total_r"] - 1.0) < 1e-9


def test_mistake_cost_counts_a_double_sourced_tag_once():
    with Session(get_engine("sqlite:///:memory:")) as s:
        t1 = _pt(-1.5)
        s.add(t1)
        s.commit()
        chased = add_tag(s, kind="mistake", name="chased")
        tag_trade(s, trade_id=t1.id, book="research", tag_id=chased.id, source="human")
        tag_trade(s, trade_id=t1.id, book="research", tag_id=chased.id, source="analyst")

        rows = mistake_cost(s, [t1])
        assert len(rows) == 1
        assert rows[0]["n"] == 1
        assert abs(rows[0]["total_r"] - (-1.5)) < 1e-9


def test_mistake_cost_ignores_open_trades_and_other_books():
    with Session(get_engine("sqlite:///:memory:")) as s:
        closed = _pt(-2.0)
        open_ = _pt(None, status="open")
        other_book = _pt(-3.0, account="paper")
        s.add_all([closed, open_, other_book])
        s.commit()
        chased = add_tag(s, kind="mistake", name="chased")
        tag_trade(s, trade_id=closed.id, book="research", tag_id=chased.id, source="human")
        tag_trade(s, trade_id=open_.id, book="research", tag_id=chased.id, source="human")
        # a link that names a different book than the trade lives in must not match
        tag_trade(s, trade_id=other_book.id, book="research", tag_id=chased.id, source="human")

        rows = mistake_cost(s, [closed, open_, other_book])
        assert len(rows) == 1
        assert rows[0]["n"] == 1  # only the closed research trade
        assert abs(rows[0]["total_r"] - (-2.0)) < 1e-9


def test_mistake_cost_is_empty_safe():
    with Session(get_engine("sqlite:///:memory:")) as s:
        assert mistake_cost(s, []) == []

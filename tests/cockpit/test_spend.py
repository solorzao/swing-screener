"""Suite-wide spend gathering: the gate must see coach/audit costs, not just analyst."""

from datetime import UTC, date, datetime

from sqlalchemy import event
from sqlalchemy.orm import Session

from swing_screener.cockpit.spend import spend_rows_since
from swing_screener.db.models import AnalystCall, JournalReview, SystemAudit
from swing_screener.db.session import get_engine

_TODAY = date(2026, 7, 12)


def test_spend_unions_all_three_tables():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(AnalystCall(created_date=_TODAY, ticker="AMD", timeframe="1d",
                          play_type="continuation", run_date=_TODAY, baseline_conviction="med",
                          final_conviction="high", nudge_reason="", model="m", est_cost_usd=0.10))
        s.add(JournalReview(identity_key="k1", kind="trade_close", book="manual_equity",
                            facts_json="{}", source="analyst", est_cost_usd=0.20,
                            generated_at=datetime(2026, 7, 12, 10, 0, tzinfo=UTC)))
        s.add(SystemAudit(kind="weekly", period_from=_TODAY, period_to=_TODAY,
                          est_cost_usd=0.05, generated_at=datetime(2026, 7, 12, 11, 0, tzinfo=UTC)))
        s.commit()
        rows = spend_rows_since(s, _TODAY)
        total = sum(c for _d, c in rows if c is not None)
        assert round(total, 2) == 0.35            # 0.10 + 0.20 + 0.05, not just the analyst


def test_null_costs_are_returned_for_counting_not_dropped():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(JournalReview(identity_key="k2", kind="trade_close", book="manual_equity",
                            facts_json="{}", source="analyst", est_cost_usd=None,
                            generated_at=datetime(2026, 7, 12, 10, 0, tzinfo=UTC)))
        s.commit()
        rows = spend_rows_since(s, _TODAY)
        assert rows == [(_TODAY, None)]           # disclosed undercount, not absorbed as 0


def test_rows_before_the_window_are_excluded():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(SystemAudit(kind="weekly", period_from=date(2026, 6, 1), period_to=date(2026, 6, 1),
                          est_cost_usd=9.0, generated_at=datetime(2026, 6, 1, 11, 0, tzinfo=UTC)))
        s.commit()
        assert spend_rows_since(s, _TODAY) == []


def test_cutoff_rides_the_sql_where_not_python():
    """/api/gate polls this constantly against Azure SQL: the date cutoff must
    reach the database as ``WHERE generated_at >= ...``, never arrive as a
    full-table scan of journal_reviews / system_audits filtered in Python.
    Behavior is pinned alongside: old rows out, new rows in."""
    engine = get_engine("sqlite:///:memory:")
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with Session(engine) as s:
        s.add(JournalReview(identity_key="old", kind="trade_close", book="manual_equity",
                            facts_json="{}", source="analyst", est_cost_usd=9.0,
                            generated_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC)))
        s.add(SystemAudit(kind="weekly", period_from=_TODAY, period_to=_TODAY,
                          est_cost_usd=0.05, generated_at=datetime(2026, 7, 12, 11, 0, tzinfo=UTC)))
        s.commit()
        statements.clear()
        rows = spend_rows_since(s, _TODAY)
    assert rows == [(_TODAY, 0.05)]  # behavior unchanged: the old row is excluded
    scans = [st for st in statements
             if "journal_reviews" in st or "system_audits" in st]
    assert scans, "the three-table union must actually query the journal tables"
    assert all("generated_at >= ?" in st for st in scans), scans

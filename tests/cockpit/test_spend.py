"""Suite-wide spend gathering: the gate must see coach/audit costs, not just analyst."""

from datetime import date, datetime

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
                            generated_at=datetime(2026, 7, 12, 10, 0)))
        s.add(SystemAudit(kind="weekly", period_from=_TODAY, period_to=_TODAY,
                          est_cost_usd=0.05, generated_at=datetime(2026, 7, 12, 11, 0)))
        s.commit()
        rows = spend_rows_since(s, _TODAY)
        total = sum(c for _d, c in rows if c is not None)
        assert round(total, 2) == 0.35            # 0.10 + 0.20 + 0.05, not just the analyst


def test_null_costs_are_returned_for_counting_not_dropped():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(JournalReview(identity_key="k2", kind="trade_close", book="manual_equity",
                            facts_json="{}", source="analyst", est_cost_usd=None,
                            generated_at=datetime(2026, 7, 12, 10, 0)))
        s.commit()
        rows = spend_rows_since(s, _TODAY)
        assert rows == [(_TODAY, None)]           # disclosed undercount, not absorbed as 0


def test_rows_before_the_window_are_excluded():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(SystemAudit(kind="weekly", period_from=date(2026, 6, 1), period_to=date(2026, 6, 1),
                          est_cost_usd=9.0, generated_at=datetime(2026, 6, 1, 11, 0)))
        s.commit()
        assert spend_rows_since(s, _TODAY) == []

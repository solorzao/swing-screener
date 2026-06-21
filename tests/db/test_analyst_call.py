"""Round-trip + default checks for the AnalystCall learning-loop store.

Each analyst conviction call is persisted so the calibration loop can later score
how the analyst's nudges actually played out. A fresh row is UNSCORED:
``realized_r`` / ``scored_at`` default to None until the outcome is graded.
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db.models import AnalystCall
from swing_screener.db.session import get_engine


def test_analyst_call_roundtrip_and_unscored_defaults() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(
            AnalystCall(
                created_date=date(2026, 6, 20),
                ticker="AMD",
                timeframe="1d",
                play_type="continuation",
                run_date=date(2026, 6, 19),
                baseline_conviction="medium",
                final_conviction="high",
                nudge_reason="sector momentum confirms the breakout",
                model="claude-opus-4-8",
            )
        )
        s.commit()
        row = s.query(AnalystCall).one()
        assert row.id is not None
        assert row.created_date == date(2026, 6, 20)
        assert row.ticker == "AMD"
        assert row.timeframe == "1d"
        assert row.play_type == "continuation"
        assert row.run_date == date(2026, 6, 19)
        assert row.baseline_conviction == "medium"
        assert row.final_conviction == "high"
        assert row.nudge_reason == "sector momentum confirms the breakout"
        assert row.model == "claude-opus-4-8"
        # fresh call is UNSCORED until the calibration loop grades the outcome.
        assert row.realized_r is None
        assert row.scored_at is None

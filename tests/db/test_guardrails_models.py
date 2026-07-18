"""Agent guardrails schema: brake-state row defaults, event append, PaperTrade.qty."""

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import AgentGuardrailEvent, AgentGuardrails, PaperTrade
from swing_screener.db.session import get_engine


def test_guardrails_row_defaults() -> None:
    with Session(get_engine("sqlite:///:memory:")) as s:
        row = AgentGuardrails(updated_at=datetime.now(UTC))
        s.add(row)
        s.commit()
        s.refresh(row)
        assert row.state == "ok"
        assert row.max_daily_loss_usd is None
        assert row.max_trades_per_day is None
        assert row.max_drawdown_usd is None
        assert row.loss_streak_halt is None
        assert row.hwm_baseline_usd == 0.0
        assert row.hwm_anchor_date is None
        assert row.trip_id is None
        assert row.trip_reason is None
        assert row.sweep_state is None


def test_guardrail_event_requires_source() -> None:
    with Session(get_engine("sqlite:///:memory:")) as s:
        ev = AgentGuardrailEvent(
            kind="edit", breaker="", reason="set max_trades_per_day=3",
            values_json="{}", source="cockpit", created_at=datetime.now(UTC),
        )
        s.add(ev)
        s.commit()
        s.refresh(ev)
        assert ev.id is not None


def test_paper_trade_qty_column() -> None:
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(PaperTrade(
            ticker="AAPL", timeframe="1d", horizon="medium", signal_score=0.81, rank=1,
            mtf_aligned=True, fill_status="filled", entry_date=date(2024, 1, 3),
            entry_price=100.5, stop=95.0, target=110.0, risk=5.5, status="open",
            qty=3,
        ))
        s.commit()
        pt = s.query(PaperTrade).filter_by(ticker="AAPL").one()
        assert pt.qty == 3

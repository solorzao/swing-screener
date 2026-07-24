"""Journal v2 foundation models: round-trip + composite-unique enforcement."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swing_screener.db.models import (
    DisarmEvent,
    JournalReview,
    SystemAudit,
    Trade,
    WeaknessesProfile,
)
from swing_screener.db.session import get_engine


def test_journal_review_roundtrip_and_unique() -> None:
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(
            JournalReview(
                identity_key="trade_close:manual_equity:1",
                kind="trade_close", book="manual_equity", trade_id=1,
                facts_json="{}", source="analyst",
            )
        )
        s.commit()
        s.add(
            JournalReview(
                identity_key="trade_close:manual_equity:1",
                kind="trade_close", book="manual_equity", trade_id=1,
                facts_json="{}", source="analyst",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_system_audit_roundtrip_and_unique() -> None:
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(
            SystemAudit(
                kind="weekly", period_from=date(2026, 7, 6),
                period_to=date(2026, 7, 12), findings_json="{}",
            )
        )
        s.commit()
        s.add(
            SystemAudit(
                kind="weekly", period_from=date(2026, 7, 6),
                period_to=date(2026, 7, 12), findings_json="{}",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_weaknesses_and_disarm_roundtrip() -> None:
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(WeaknessesProfile(scope="personal", items_json="[]"))
        s.add(DisarmEvent(created_at=datetime(2026, 7, 12, 14, 0, tzinfo=UTC), reason="manual"))
        s.commit()
        assert s.query(WeaknessesProfile).count() == 1
        assert s.query(DisarmEvent).count() == 1


def test_trade_emotional_state_column() -> None:
    with Session(get_engine("sqlite:///:memory:")) as s:
        t = Trade(
            ticker="AMD", timeframe="1d", horizon="medium", entry_date=date(2026, 7, 1),
            entry_price=100.0, size=1.0, stop=95.0, target=110.0, emotional_state="calm",
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        assert t.emotional_state == "calm"

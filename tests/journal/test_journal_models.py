"""Task 6 test contract: the three journal annotation tables (tags, notes, theses)
plus the tag-definition table, over an in-memory ``get_engine`` roundtrip.

Two invariants under test: (1) every column persists and reads back unchanged
(bounded strings, DateTime lifecycles, Text bodies); (2) ``source`` provenance is
REQUIRED -- a trade-tag / note / thesis written without it fails at the database
(NOT NULL), so provenance can never be silently lost (the shadow-contamination
lesson).
"""

from datetime import date, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swing_screener.db.models import (
    JournalNote,
    JournalTag,
    JournalThesis,
    JournalTradeTag,
)
from swing_screener.db.session import get_engine


def _engine():
    return get_engine("sqlite:///:memory:")


def test_tag_and_trade_tag_roundtrip():
    engine = _engine()
    with Session(engine) as s:
        tag = JournalTag(kind="mistake", name="chased", description="entered too extended")
        s.add(tag)
        s.flush()  # assign tag.id
        s.add(JournalTradeTag(
            trade_id=42, book="research", tag_id=tag.id, source="human",
            created_at=datetime(2026, 7, 12, 14, 30, 0),  # noqa: DTZ001 -- naive literal; SQLite DateTime column is tz-naive on roundtrip
        ))
        s.commit()
        tag_id = tag.id

    with Session(engine) as s:
        got_tag = s.get(JournalTag, tag_id)
        assert got_tag is not None
        assert got_tag.kind == "mistake"
        assert got_tag.name == "chased"
        assert got_tag.description == "entered too extended"

        link = s.query(JournalTradeTag).one()
        assert link.trade_id == 42
        assert link.book == "research"
        assert link.tag_id == tag_id
        assert link.source == "human"
        assert link.created_at == datetime(2026, 7, 12, 14, 30, 0)  # noqa: DTZ001 -- naive literal; SQLite DateTime column is tz-naive on roundtrip


def test_note_roundtrip():
    engine = _engine()
    with Session(engine) as s:
        s.add(JournalNote(
            day=date(2026, 7, 12), kind="premarket", module="swing",
            body="watching semis into CPI", source="human",
            created_at=datetime(2026, 7, 12, 8, 0, 0),  # noqa: DTZ001 -- naive literal; SQLite DateTime column is tz-naive on roundtrip
        ))
        s.commit()

    with Session(engine) as s:
        note = s.query(JournalNote).one()
        assert note.day == date(2026, 7, 12)
        assert note.kind == "premarket"
        assert note.module == "swing"
        assert note.body == "watching semis into CPI"
        assert note.source == "human"
        assert note.created_at == datetime(2026, 7, 12, 8, 0, 0)  # noqa: DTZ001 -- naive literal; SQLite DateTime column is tz-naive on roundtrip


def test_note_module_is_optional():
    engine = _engine()
    with Session(engine) as s:
        s.add(JournalNote(day=date(2026, 7, 12), kind="adhoc", body="x", source="human"))
        s.commit()

    with Session(engine) as s:
        note = s.query(JournalNote).one()
        assert note.module is None


def test_thesis_roundtrip_and_snapshot_default():
    engine = _engine()
    with Session(engine) as s:
        s.add(JournalThesis(
            trade_id=7, book="research", event_kind="entry", source="screener",
            body="pullback into rising 20EMA", snapshot_json='{"rsi": 44}',
        ))
        # a second thesis without an explicit snapshot uses the "{}" default
        s.add(JournalThesis(
            trade_id=7, book="research", event_kind="exit", source="analyst",
            body="target reached",
        ))
        s.commit()

    with Session(engine) as s:
        entry = s.query(JournalThesis).filter_by(event_kind="entry").one()
        assert entry.trade_id == 7
        assert entry.book == "research"
        assert entry.source == "screener"
        assert entry.body == "pullback into rising 20EMA"
        assert entry.snapshot_json == '{"rsi": 44}'
        exit_ = s.query(JournalThesis).filter_by(event_kind="exit").one()
        assert exit_.snapshot_json == "{}"


@pytest.mark.parametrize("factory", [
    lambda: JournalTradeTag(trade_id=1, book="research", tag_id=1),  # no source
    lambda: JournalNote(day=date(2026, 7, 12), kind="adhoc", body="x"),  # no source
    lambda: JournalThesis(trade_id=1, book="research", event_kind="entry", body="x"),  # no source
])
def test_source_is_required(factory):
    """Provenance is not optional: a NULL ``source`` is an IntegrityError, never a
    silent write -- the annotation must always know who made it."""
    engine = _engine()
    with Session(engine) as s:
        s.add(factory())
        with pytest.raises(IntegrityError):
            s.commit()

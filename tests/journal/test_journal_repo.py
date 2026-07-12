"""Task 7 test contract: plain session-first repo functions for the journal tables.

``source`` is a REQUIRED keyword everywhere provenance is written (tag_trade,
add_note, add_thesis) -- no default -- so a caller can never silently drop it.
Idempotency where it matters: a (trade, book, tag, source) application is unique;
re-applying returns the same row instead of duplicating. Tag DEFINITIONS are
get-or-create on (kind, name) so the vocabulary never accretes duplicates.
"""

from datetime import date, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import JournalTradeTag
from swing_screener.db.session import get_engine
from swing_screener.journal.repo import (
    add_note,
    add_tag,
    add_thesis,
    list_trade_tags,
    notes_for_day,
    tag_trade,
    theses_for_trade,
)


def _session() -> Session:
    return Session(get_engine("sqlite:///:memory:"))


# --- tags -------------------------------------------------------------------

def test_add_tag_is_get_or_create_on_kind_and_name():
    with _session() as s:
        a = add_tag(s, kind="mistake", name="chased", description="too extended")
        assert a.id is not None
        # same (kind, name) -> the SAME row, not a duplicate; description not clobbered
        b = add_tag(s, kind="mistake", name="chased", description="ignored")
        assert b.id == a.id
        assert b.description == "too extended"
        # a different kind with the same name is a distinct tag
        c = add_tag(s, kind="context", name="chased")
        assert c.id != a.id


def test_tag_trade_writes_provenance_and_is_idempotent_per_source():
    with _session() as s:
        tag = add_tag(s, kind="mistake", name="moved_stop")
        first = tag_trade(s, trade_id=10, book="research", tag_id=tag.id, source="human")
        again = tag_trade(s, trade_id=10, book="research", tag_id=tag.id, source="human")
        assert again.id == first.id  # idempotent: same (trade, book, tag, source)
        assert s.query(JournalTradeTag).count() == 1
        # a different source on the same pair is a genuinely different application
        analyst = tag_trade(s, trade_id=10, book="research", tag_id=tag.id, source="analyst")
        assert analyst.id != first.id
        assert s.query(JournalTradeTag).count() == 2


def test_list_trade_tags_returns_link_rows_for_the_trade_and_book():
    with _session() as s:
        t1 = add_tag(s, kind="mistake", name="chased")
        t2 = add_tag(s, kind="setup", name="pullback")
        tag_trade(s, trade_id=5, book="research", tag_id=t1.id, source="human")
        tag_trade(s, trade_id=5, book="research", tag_id=t2.id, source="screener")
        tag_trade(s, trade_id=5, book="paper", tag_id=t1.id, source="human")  # other book
        tag_trade(s, trade_id=6, book="research", tag_id=t1.id, source="human")  # other trade

        got = list_trade_tags(s, trade_id=5, book="research")
        assert {(g.tag_id, g.source) for g in got} == {(t1.id, "human"), (t2.id, "screener")}


# --- notes ------------------------------------------------------------------

def test_add_note_requires_source_and_notes_for_day_filters_by_day():
    with _session() as s:
        add_note(s, day=date(2026, 7, 12), kind="premarket", body="plan", source="human")
        add_note(s, day=date(2026, 7, 12), kind="postmarket", body="review",
                 source="human", module="swing")
        add_note(s, day=date(2026, 7, 13), kind="adhoc", body="other day", source="human")

        today = notes_for_day(s, day=date(2026, 7, 12))
        assert [n.kind for n in today] == ["premarket", "postmarket"]
        assert today[1].module == "swing"
        assert notes_for_day(s, day=date(2026, 7, 11)) == []


# --- theses -----------------------------------------------------------------

def test_add_thesis_and_theses_for_trade():
    with _session() as s:
        add_thesis(s, trade_id=9, book="research", event_kind="entry", source="screener",
                   body="pullback into 20EMA", snapshot_json='{"rsi": 40}',
                   created_at=datetime(2026, 7, 12, 9, 30, 0))
        add_thesis(s, trade_id=9, book="research", event_kind="exit", source="human",
                   body="hit target")
        add_thesis(s, trade_id=9, book="paper", event_kind="entry", source="human",
                   body="other book")

        got = theses_for_trade(s, trade_id=9, book="research")
        assert [t.event_kind for t in got] == ["entry", "exit"]
        assert got[0].snapshot_json == '{"rsi": 40}'
        assert got[1].snapshot_json == "{}"  # default when omitted

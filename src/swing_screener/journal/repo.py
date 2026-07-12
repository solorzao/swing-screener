"""CRUD for the journal annotation tables (tags, trade-tags, notes, theses).

Plain ``session``-first functions in the house repo style (mirrors
``swing_screener.db.repo``), kept in the journal package because they belong to the
journal read-model, not the core screener store. ``source`` provenance is a REQUIRED
keyword on every writer that records it (tag_trade, add_note, add_thesis) -- no
default -- so an annotation's origin can never be silently lost.

Two idempotency rules, deliberate:

* ``add_tag`` is get-or-create on ``(kind, name)``: the shared vocabulary never
  accretes duplicate definitions (re-adding an existing tag returns it, leaving its
  description untouched).
* ``tag_trade`` is get-or-create on ``(trade_id, book, tag_id, source)``: applying
  the same tag from the same source twice is one row, but the SAME tag from a
  DIFFERENT source (human vs analyst) is a genuinely distinct application.

Notes and theses are event records -- they accumulate, never dedupe.
"""

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import (
    JournalNote,
    JournalTag,
    JournalThesis,
    JournalTradeTag,
)


def add_tag(
    session: Session, *, kind: str, name: str, description: str = ""
) -> JournalTag:
    """Get-or-create a tag definition by ``(kind, name)``. Re-adding an existing tag
    returns it unchanged (the stored ``description`` wins -- a second add never
    clobbers it)."""
    existing = session.scalar(
        select(JournalTag).where(JournalTag.kind == kind, JournalTag.name == name)
    )
    if existing is not None:
        return existing
    tag = JournalTag(kind=kind, name=name, description=description)
    session.add(tag)
    session.commit()
    session.refresh(tag)
    return tag


def tag_trade(
    session: Session,
    *,
    trade_id: int,
    book: str,
    tag_id: int,
    source: str,
    created_at: datetime | None = None,
) -> JournalTradeTag:
    """Apply ``tag_id`` to a trade, with provenance. Idempotent on
    ``(trade_id, book, tag_id, source)``: a re-application returns the existing row
    (leaving its ``created_at`` untouched); the same tag from a different ``source``
    is a new application."""
    existing = session.scalar(
        select(JournalTradeTag).where(
            JournalTradeTag.trade_id == trade_id,
            JournalTradeTag.book == book,
            JournalTradeTag.tag_id == tag_id,
            JournalTradeTag.source == source,
        )
    )
    if existing is not None:
        return existing
    link = JournalTradeTag(
        trade_id=trade_id, book=book, tag_id=tag_id, source=source,
        created_at=created_at,
    )
    session.add(link)
    session.commit()
    session.refresh(link)
    return link


def list_trade_tags(
    session: Session, *, trade_id: int, book: str
) -> list[JournalTradeTag]:
    """Every tag APPLICATION on ``(trade_id, book)`` -- the link rows, so provenance
    (``source``) and ``tag_id`` are preserved for the caller to resolve to names."""
    stmt = (
        select(JournalTradeTag)
        .where(JournalTradeTag.trade_id == trade_id, JournalTradeTag.book == book)
        .order_by(JournalTradeTag.id)
    )
    return list(session.scalars(stmt))


def add_note(
    session: Session,
    *,
    day: date,
    kind: str,
    body: str,
    source: str,
    module: str | None = None,
    created_at: datetime | None = None,
) -> JournalNote:
    """Append a day-keyed notebook entry. ``source`` is required; notes accumulate
    (never deduped) -- a day can hold many premarket/postmarket/adhoc entries."""
    note = JournalNote(
        day=day, kind=kind, body=body, source=source, module=module,
        created_at=created_at,
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def notes_for_day(session: Session, *, day: date) -> list[JournalNote]:
    """Every note for ``day`` in insertion order (id-ascending)."""
    stmt = select(JournalNote).where(JournalNote.day == day).order_by(JournalNote.id)
    return list(session.scalars(stmt))


def add_thesis(
    session: Session,
    *,
    trade_id: int,
    book: str,
    event_kind: str,
    source: str,
    body: str,
    snapshot_json: str = "{}",
    created_at: datetime | None = None,
) -> JournalThesis:
    """Record an entry/exit thesis for a trade. ``source`` is required; theses
    accumulate (a trade may carry both an entry and an exit thesis, and later
    re-reads)."""
    thesis = JournalThesis(
        trade_id=trade_id, book=book, event_kind=event_kind, source=source,
        body=body, snapshot_json=snapshot_json, created_at=created_at,
    )
    session.add(thesis)
    session.commit()
    session.refresh(thesis)
    return thesis


def theses_for_trade(
    session: Session, *, trade_id: int, book: str
) -> list[JournalThesis]:
    """Every thesis on ``(trade_id, book)`` in insertion order (id-ascending)."""
    stmt = (
        select(JournalThesis)
        .where(JournalThesis.trade_id == trade_id, JournalThesis.book == book)
        .order_by(JournalThesis.id)
    )
    return list(session.scalars(stmt))

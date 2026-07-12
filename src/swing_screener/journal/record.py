"""The unified TradeRecord read-model -- a display-only normalization over the swing
book's ``PaperTrade`` rows, with each trade's tags and theses attached.

DISPLAY ONLY. This never aggregates: statistics stay per-book in the analytics
functions (``summarize`` / ``breakdown`` / the journal report modules). A TradeRecord
is one row a journal UI renders -- symbol, direction, dates, its R result, and the
human/analyst/screener annotations hanging off it. It is R-native for the swing source
(``unit="R"``, ``module="swing"``, long convention); the shape is deliberately source-
agnostic so a future GEX ``OptionPaperTrade`` folds in as a second producer
(``module="gex"``, a varying unit) without any consumer changing.

Firewall: ``book`` selects one account and the tag/thesis joins are book-scoped, so a
record never mixes books and an annotation can't leak across them.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import (
    JournalTag,
    JournalThesis,
    JournalTradeTag,
    PaperTrade,
)


@dataclass(frozen=True)
class TagView:
    """A tag as displayed on a record: its name + kind, plus who applied it."""

    name: str
    kind: str
    source: str


@dataclass(frozen=True)
class ThesisView:
    """A thesis as displayed on a record: which event, who wrote it, the prose."""

    event_kind: str
    source: str
    body: str


@dataclass(frozen=True)
class TradeRecord:
    """One display row over a swing ``PaperTrade`` with its annotations attached."""

    book: str
    module: str
    trade_id: int
    symbol: str
    direction: str
    opened: date | None
    closed: date | None
    unit: str
    result: float | None  # interpreted by ``unit`` ("R" for swing/manual, "$" for robinhood)
    tags: list[TagView] = field(default_factory=list)
    theses: list[ThesisView] = field(default_factory=list)


def trade_records(session: Session, *, book: str) -> list[TradeRecord]:
    """Every ``PaperTrade`` in ``book`` (its account) as a display TradeRecord, in
    stable id order, each carrying its book-scoped tags and theses.

    ``opened`` prefers ``opened_date`` and falls back to ``entry_date`` (a legacy row
    may carry only the fill date); ``closed`` is ``exit_date`` (None while open);
    ``result`` is ``realized_r`` (None until closed). Tags/theses are display views in
    application order. Pure read; no aggregation. Empty book -> ``[]``.
    """
    trades = list(session.scalars(
        select(PaperTrade).where(PaperTrade.account == book).order_by(PaperTrade.id)
    ))
    if not trades:
        return []

    trade_ids = [t.id for t in trades]
    tags_by_trade = _tags_by_trade(session, book=book, trade_ids=trade_ids)
    theses_by_trade = _theses_by_trade(session, book=book, trade_ids=trade_ids)

    return [
        TradeRecord(
            book=book,
            module="swing",
            trade_id=t.id,
            symbol=t.ticker,
            direction="long",  # the swing book is long-only
            opened=t.opened_date if t.opened_date is not None else t.entry_date,
            closed=t.exit_date,
            unit="R",
            result=t.realized_r,
            tags=tags_by_trade.get(t.id, []),
            theses=theses_by_trade.get(t.id, []),
        )
        for t in trades
    ]


def _tags_by_trade(
    session: Session, *, book: str, trade_ids: Iterable[int]
) -> dict[int, list[TagView]]:
    """``{trade_id: [TagView, ...]}`` for the book, one query for the links + one for
    the (small) tag vocabulary -- no per-trade round-trips."""
    tag_meta = {
        tag.id: (tag.name, tag.kind) for tag in session.scalars(select(JournalTag))
    }
    stmt = (
        select(JournalTradeTag)
        .where(JournalTradeTag.book == book, JournalTradeTag.trade_id.in_(trade_ids))
        .order_by(JournalTradeTag.id)
    )
    out: dict[int, list[TagView]] = {}
    for link in session.scalars(stmt):
        name, kind = tag_meta.get(link.tag_id, ("?", "?"))
        out.setdefault(link.trade_id, []).append(
            TagView(name=name, kind=kind, source=link.source)
        )
    return out


def _theses_by_trade(
    session: Session, *, book: str, trade_ids: Iterable[int]
) -> dict[int, list[ThesisView]]:
    """``{trade_id: [ThesisView, ...]}`` for the book, in application order."""
    stmt = (
        select(JournalThesis)
        .where(JournalThesis.book == book, JournalThesis.trade_id.in_(trade_ids))
        .order_by(JournalThesis.id)
    )
    out: dict[int, list[ThesisView]] = {}
    for th in session.scalars(stmt):
        out.setdefault(th.trade_id, []).append(
            ThesisView(event_kind=th.event_kind, source=th.source, body=th.body)
        )
    return out

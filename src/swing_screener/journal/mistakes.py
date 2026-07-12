"""Mistake-cost report: what each confessed mistake actually cost, in realized R.

Joins ``kind="mistake"`` tags (applied by a HUMAN or the ANALYST -- a screener-applied
tag is denormalized metadata, not a confession) to the trades they mark, groups by
mistake name, and reports the realized cost (sum R) and count with an honest
``summarize`` per group so each row carries clustered CIs, not a bare number
("chasing cost -4.2R over 6 trades"). Only closed-filled trades contribute -- an open
trade has no realized cost yet -- and a trade tagged the same mistake by two sources
counts once. Rows come back worst-first (most negative total R). None/empty-safe:
no trades, or none tagged, yields ``[]``.

Firewall: the caller passes the trade cohort for ONE book (R-native swing rows); a
tag link matches a trade only when its ``book`` equals the trade's ``account``, so a
tag can never bleed across books.
"""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import _is_closed_filled, summarize
from swing_screener.db.models import JournalTag, JournalTradeTag, PaperTrade

# The provenance a confessed mistake must carry: a human owning it, or the analyst
# flagging it. Screener-denormalized tags are excluded -- they are not admissions.
_MISTAKE_SOURCES = ("human", "analyst")


def mistake_cost(session: Session, trades: Iterable[PaperTrade]) -> list[dict]:
    """Per-mistake realized cost over ``trades``, worst-first.

    Each row: ``{"mistake": name, "n": int, "total_r": float, "summary":
    PerformanceSummary}`` -- ``n``/``total_r`` over the closed-filled trades tagged
    with that mistake, ``summary`` the reused clustered-CI aggregate for the same
    group. Ordered by ``total_r`` ascending (the biggest drag first). Only
    ``kind="mistake"`` tags from ``human``/``analyst`` count; a trade double-tagged
    (two sources) counts once; open/unfilled trades never contribute. ``[]`` when
    nothing qualifies.
    """
    # Index the cohort by (id, book) so a tag matches only within its own book.
    trade_by_key: dict[tuple[int, str], PaperTrade] = {
        (t.id, t.account): t
        for t in trades
        if t.id is not None and _is_closed_filled(t)
    }
    if not trade_by_key:
        return []

    trade_ids = {tid for tid, _ in trade_by_key}
    stmt = (
        select(JournalTradeTag.trade_id, JournalTradeTag.book, JournalTag.name)
        .join(JournalTag, JournalTradeTag.tag_id == JournalTag.id)
        .where(
            JournalTag.kind == "mistake",
            JournalTradeTag.source.in_(_MISTAKE_SOURCES),
            JournalTradeTag.trade_id.in_(trade_ids),
        )
    )

    # name -> {(id, book): trade}, deduping a trade tagged by multiple sources.
    groups: dict[str, dict[tuple[int, str], PaperTrade]] = {}
    for trade_id, book, name in session.execute(stmt):
        trade = trade_by_key.get((trade_id, book))
        if trade is not None:
            groups.setdefault(name, {})[(trade_id, book)] = trade

    rows: list[dict] = []
    for name, members in groups.items():
        cohort = list(members.values())
        # every member is closed-filled by construction, so realized_r is present
        total_r = sum(t.realized_r for t in cohort if t.realized_r is not None)
        rows.append({
            "mistake": name,
            "n": len(cohort),
            "total_r": total_r,
            "summary": summarize(cohort),
        })
    rows.sort(key=lambda row: row["total_r"])
    return rows

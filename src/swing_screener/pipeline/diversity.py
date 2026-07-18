"""Surface-selection helpers for the daily digest lists.

Pure helpers shared by the digest selectors (``notify.select``) and the screen's
chart pickers (``pipeline.run._digest_chart_indices`` / ``_reversal_chart_indices``)
so the same picks are surfaced and charted: the sector-diversity cap and the
per-ticker dedup. No I/O.
"""

from collections.abc import Callable, Iterable
from typing import TypeVar

T = TypeVar("T")


def first_per_ticker(rows: Iterable[T], ticker_of: Callable[[T], str]) -> list[T]:
    """Keep only the FIRST row per ticker, preserving the input order.

    Callers iterate in rank/score order, so the kept row is the ticker's BEST one.
    Without this, one ticker firing on multiple timeframes (common in a strong
    trend) fills multiple top-N slots -- and each surfaced slot is a billable Opus
    deep/conviction call, so the same name was pitched (and billed) twice. Callers
    over-fetch (no SQL LIMIT) and trim AFTER the dedup, so freed slots backfill
    from below in rank order rather than shrinking the list. Shared with the chart
    pickers so the chart set agrees with the digest about which TICKERS won slots."""
    seen: set[str] = set()
    out: list[T] = []
    for row in rows:
        ticker = ticker_of(row)
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append(row)
    return out


def cap_by_sector(
    items: Iterable[T],
    sector_of: Callable[[T], str | None],
    *,
    max_per_sector: int,
    limit: int,
) -> list[T]:
    """Take up to ``limit`` items in their given order, keeping at most
    ``max_per_sector`` per sector.

    ``sector_of(item)`` returns the item's sector, or None/"" when unknown. An item
    with no sector is NEVER capped (each stays eligible) -- fail-open, so missing sector
    data can never hide a top pick. ``items`` should already be in priority order (e.g.
    by rank/score); order is otherwise preserved.
    """
    out: list[T] = []
    counts: dict[str, int] = {}
    for it in items:
        if len(out) >= limit:
            break
        sector = sector_of(it)
        if sector:
            if counts.get(sector, 0) >= max_per_sector:
                continue
            counts[sector] = counts.get(sector, 0) + 1
        out.append(it)
    return out

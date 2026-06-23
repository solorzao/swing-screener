"""Sector-diversity cap for the daily surface.

Pure helper shared by the digest selector (``notify.select.daily_picks``) and the
screen's chart picker (``pipeline.run._digest_chart_indices``) so the same picks are
surfaced and charted. No I/O.
"""

from collections.abc import Callable, Iterable
from typing import TypeVar

T = TypeVar("T")


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

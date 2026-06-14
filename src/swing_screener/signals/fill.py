from dataclasses import dataclass
from typing import Literal

from swing_screener.signals.entry_zone import EntryZone

FillStatus = Literal["filled", "missed", "invalidated"]


@dataclass(frozen=True)
class FillResult:
    status: FillStatus
    price: float | None   # worst-case in-zone long fill, or None


def resolve_fill(zone: EntryZone, bar_high: float, bar_low: float) -> FillResult:
    # invalidated: the bar gapped/traded below the stop before we could enter in-zone
    if bar_low < zone.stop and bar_high < zone.floor:
        return FillResult("invalidated", None)
    # missed: the bar never came down into the zone (gapped above the ceiling)
    if bar_low > zone.ceiling:
        return FillResult("missed", None)
    # filled: some overlap with [floor, ceiling]; worst case for a long = highest in-zone price
    if bar_high < zone.floor:
        return FillResult("invalidated", None)
    price = min(bar_high, zone.ceiling)
    return FillResult("filled", price)

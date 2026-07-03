from dataclasses import dataclass
from typing import Literal

from swing_screener.signals.entry_zone import EntryZone

FillStatus = Literal["filled", "missed", "invalidated"]


@dataclass(frozen=True)
class FillResult:
    status: FillStatus
    price: float | None   # worst-case in-zone long fill, or None


def resolve_fill(zone: EntryZone, bar_high: float, bar_low: float) -> FillResult:
    # KNOWN, ACCEPTED BIAS (2026-07 audit): a bar that sweeps the zone AND breaks the stop
    # (bar_high >= floor, bar_low < stop) resolves "filled" at the worst in-zone price, and
    # the stepper never evaluates the entry bar -- so a same-bar fill+stop is carried to the
    # NEXT bar instead of booking ~-1R immediately. Intrabar ordering is unknowable from
    # OHLC, so this reads slightly optimistic for the fill-bar stop case; fixing it means
    # either pessimistically assuming stop-first or persisting the fill bar for the stepper.
    # Left as-is deliberately -- documented so the bias is priced into how results are read.
    #
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

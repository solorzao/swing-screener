"""Live actionability of a surfaced signal -- "is this still buyable, or did it run?"

A signal is computed at the trigger bar's close and surfaced later (next morning in
the digest, or days later in the dashboard). By then the live price may have left the
entry zone: it can have run up past the ceiling (an "already ran" chase) or broken
below the stop (the setup failed). This pure helper classifies a signal's entry zone
against a current price so the surface can flag/sort/hide stale picks.

Pure: no I/O. The caller supplies the live price (e.g. the latest close).
"""

from dataclasses import dataclass
from typing import Literal

# actionable: price is in or below the zone (a fill is still reachable, room to enter)
# extended:   price ran above the entry ceiling by more than the buffer -- "already ran"
# broken:     price is at/below the stop -- the setup has already failed
# unknown:    no live price, or a degenerate zone
Status = Literal["actionable", "extended", "broken", "unknown"]


@dataclass(frozen=True)
class Actionability:
    status: Status
    # How far the live price sits past the entry ceiling, measured in units of the
    # zone's own risk (ceiling -> stop). <= 0 means price is still at/below the zone
    # (room to enter); > 0 means it has run above the buy area. None when unknown.
    dist_r: float | None


def classify(
    *,
    entry_floor: float,
    entry_ceiling: float,
    stop: float,
    price: float | None,
    buffer_r: float = 0.25,
) -> Actionability:
    """Classify a signal's entry zone against the current ``price``.

    ``buffer_r`` is how far above the ceiling (in zone-risk units) still counts as
    actionable before a pick is called "extended" -- a small tolerance so a price a
    hair above the ceiling isn't prematurely written off. ``entry_floor`` is accepted
    for a complete zone description though the classification keys off ceiling/stop.
    """
    risk = entry_ceiling - stop
    if price is None or risk <= 0:
        return Actionability("unknown", None)

    dist_r = (price - entry_ceiling) / risk
    if price <= stop:
        return Actionability("broken", dist_r)
    if dist_r > buffer_r:
        return Actionability("extended", dist_r)
    return Actionability("actionable", dist_r)

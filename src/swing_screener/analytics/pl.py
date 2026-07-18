"""Unrealized P/L math for open positions.

Pure arithmetic over a trade's entry/stop/target and a current price -- no I/O.
Consumed by the cockpit's open-trades endpoint (routers/trades.py).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionPL:
    unrealized_pl: float       # currency P/L
    unrealized_pct: float      # fraction of entry
    r_multiple: float          # (price - entry) / risk
    dist_to_stop_pct: float    # (price - stop) / price
    dist_to_target_pct: float  # (target - price) / price


def position_pl(*, entry: float, stop: float, target: float, size: float,
                current_price: float) -> PositionPL:
    """Unrealized P/L for an open long position. Raises if risk (entry - stop) <= 0."""
    risk = entry - stop
    if risk <= 0:
        raise ValueError(f"risk (entry - stop) must be positive, got {risk}")
    return PositionPL(
        unrealized_pl=(current_price - entry) * size,
        unrealized_pct=(current_price - entry) / entry,
        r_multiple=(current_price - entry) / risk,
        dist_to_stop_pct=(current_price - stop) / current_price,
        dist_to_target_pct=(target - current_price) / current_price,
    )

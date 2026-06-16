from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from swing_screener.db.models import Trade


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


def total_unrealized_pl(trades: Iterable[Trade], prices: Mapping[str, float | None]) -> float:
    """Sum unrealized P/L over trades, skipping ones with a missing quote or non-positive risk."""
    total = 0.0
    for t in trades:
        price = prices.get(t.ticker)
        if price is None:
            continue
        try:
            total += position_pl(entry=t.entry_price, stop=t.stop, target=t.target,
                                 size=t.size, current_price=price).unrealized_pl
        except ValueError:
            continue
    return total

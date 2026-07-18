"""Deterministic level math for the lab: swing-pivot support/resistance and
Fibonacci retracement. All inputs are STANDARD candle highs/lows (never HA --
HA smears real extremes, the repo-wide invariant).

The pivot rule matches signals.entry_zone.nearest_resistance: a pivot tops (or
bottoms) ``width`` bars on each side with strict inequality, so flat double
tops/bottoms are deliberately not pivots (conservative by design). The lab
generalises it to BOTH sides and to a clustered level list instead of a single
nearest price.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Level:
    price: float   # mean price of the clustered pivots
    touches: int   # pivots merged into this level


def pivot_highs(highs: list[float], width: int) -> list[float]:
    """Swing-high pivot prices (oldest->newest); strict > over width bars each side."""
    return [
        highs[i]
        for i in range(width, len(highs) - width)
        if all(highs[i] > highs[i - k] and highs[i] > highs[i + k]
               for k in range(1, width + 1))
    ]


def pivot_lows(lows: list[float], width: int) -> list[float]:
    """Swing-low pivot prices (oldest->newest); strict < over width bars each side."""
    return [
        lows[i]
        for i in range(width, len(lows) - width)
        if all(lows[i] < lows[i - k] and lows[i] < lows[i + k]
               for k in range(1, width + 1))
    ]


def cluster_levels(pivots: list[float], tol_frac: float) -> list[Level]:
    """Greedy price clustering: pivots within ``tol_frac`` of the running cluster
    mean merge into one level. Returns levels sorted by price ascending."""
    out: list[Level] = []
    members: list[float] = []
    for p in sorted(pivots):
        if members:
            mean = sum(members) / len(members)
            if mean > 0 and abs(p - mean) / mean <= tol_frac:
                members.append(p)
                continue
            out.append(Level(price=sum(members) / len(members), touches=len(members)))
        members = [p]
    if members:
        out.append(Level(price=sum(members) / len(members), touches=len(members)))
    return out


def sr_levels(highs: list[float], lows: list[float], last_close: float, *,
              width: int = 3, tol_frac: float = 0.005,
              max_per_side: int = 4) -> dict[str, list[Level]]:
    """Support/resistance from pooled swing pivots. High and low pivots cluster
    together (a broken resistance acts as support and vice versa); the split is
    by the last close -- levels above it resist, levels below support. Each side
    is nearest-first and capped at ``max_per_side``."""
    pooled = pivot_highs(highs, width) + pivot_lows(lows, width)
    clustered = cluster_levels(pooled, tol_frac)
    resistance = sorted(
        (lv for lv in clustered if lv.price > last_close), key=lambda lv: lv.price
    )
    support = sorted(
        (lv for lv in clustered if lv.price <= last_close),
        key=lambda lv: lv.price, reverse=True,
    )
    return {
        "support": support[:max_per_side],
        "resistance": resistance[:max_per_side],
    }


# The classic retracement set. 0.0 is the swing the move ended at, 1.0 the swing
# it started from, so 0.382 always reads "38.2% given back".
FIB_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)


def fib_retracement(highs: list[float], lows: list[float]) -> dict[str, Any] | None:
    """Fibonacci retracement of the window's dominant swing: window high to
    window low, direction by which extreme printed LATER (high after low ->
    up-move being retraced, and vice versa). None on a degenerate window."""
    if not highs or not lows:
        return None
    hi = max(highs)
    lo = min(lows)
    span = hi - lo
    if not span > 0:
        return None
    hi_i = max(i for i, h in enumerate(highs) if h == hi)
    lo_i = max(i for i, low in enumerate(lows) if low == lo)
    direction = "up" if hi_i >= lo_i else "down"
    if direction == "up":
        levels = [{"ratio": r, "price": hi - r * span} for r in FIB_RATIOS]
    else:
        levels = [{"ratio": r, "price": lo + r * span} for r in FIB_RATIOS]
    return {"high": hi, "low": lo, "direction": direction, "levels": levels}

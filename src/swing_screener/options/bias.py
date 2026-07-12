"""9/21/50 EMA stack state: the strategy's trend gauge.

A clean stack (fast > mid > slow, meaningfully separated, with the fast EMA
sloping the same way) is the lab's directional green light; anything else is
"tangled" -- no edge, stand down. Spacing between the fast and slow EMA is a
momentum proxy (wider = stronger), per docs/modules/gex-lab.md.
"""

from dataclasses import dataclass

import pandas as pd

from swing_screener.indicators.trend import ema
from swing_screener.options.config import GexConfig


@dataclass(frozen=True)
class StackState:
    direction: str  # bullish | bearish | tangled
    spacing_pct: float
    emas: tuple[float, float, float]


def stack_state(close: pd.Series, cfg: GexConfig) -> StackState:
    """Classify the 9/21/50 EMA stack on a close-price series.

    Bullish iff fast > mid > slow, the fast/slow spacing clears
    ``min_stack_spacing_pct`` (a tight, near-coincident stack is chop even if
    momentarily ordered), AND the fast EMA is higher than ``slope_lookback`` bars
    ago; bearish is the mirror; everything else -- including a series too short to
    seat the slowest EMA twice -- is tangled.

    The slope is measured endpoint-to-endpoint on purpose: a 1-bar wobble inside a
    real trend (exactly the pullback this strategy trades) must not flip the daily
    bias to chop. Coincident-stack detection, not a per-bar slope, is what rejects
    an oscillation.
    """
    fast_span, mid_span, slow_span = cfg.ema_spans
    fast = ema(close, fast_span)
    mid = ema(close, mid_span)
    slow = ema(close, slow_span)
    f, m, s = float(fast.iloc[-1]), float(mid.iloc[-1]), float(slow.iloc[-1])
    last_close = float(close.iloc[-1])
    spacing_pct = abs(f - s) / last_close * 100.0 if last_close else 0.0
    emas = (f, m, s)

    if len(close) < max(cfg.ema_spans) * 2 or spacing_pct < cfg.min_stack_spacing_pct:
        return StackState("tangled", spacing_pct, emas)

    fast_then = float(fast.iloc[-1 - cfg.slope_lookback])
    if f > m > s and f > fast_then:
        direction = "bullish"
    elif f < m < s and f < fast_then:
        direction = "bearish"
    else:
        direction = "tangled"
    return StackState(direction, spacing_pct, emas)

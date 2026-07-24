from collections.abc import Sequence
from dataclasses import dataclass

from swing_screener.config import StrategyConfig


@dataclass(frozen=True)
class EntryZone:
    floor: float
    ceiling: float
    stop: float
    target: float
    risk: float       # reference_entry - stop (per share)
    reference: float  # entry anchor for R math == the buy-at-or-below ceiling (the fill)


def nearest_resistance(highs: Sequence[float], above: float, width: int) -> float | None:
    """Lowest swing-high pivot strictly above ``above``. A pivot at i tops ``width`` bars on
    each side. Returns None if no qualifying pivot. ``highs`` are STANDARD candle highs,
    oldest->newest.

    The strict ``>`` on both sides means a FLAT top (a double-top with equal adjacent highs)
    is deliberately NOT counted as a pivot -- such a level falls through to the ATR-measured
    fallback rather than being treated as confirmed resistance. Conservative by design.
    """
    candidates: list[float] = []
    for i in range(width, len(highs) - width):
        h = highs[i]
        if all(h > highs[i - k] and h > highs[i + k] for k in range(1, width + 1)) and h > above:
            candidates.append(h)
    if not candidates:
        return None
    return min(candidates)


def compute_zone(trigger_close: float, atr: float, swing_low: float,
                 cfg: StrategyConfig,
                 recent_highs: Sequence[float] | None = None) -> EntryZone | None:
    """Compute the entry zone, or None when the zone is degenerate.

    A shallow pullback (swing_low at or above trigger_close) combined with a
    large ATR can push the floor above the ceiling, producing an inverted zone
    where ``floor >= ceiling``. An inverted zone would make resolve_fill
    mis-classify in-zone bars, and a non-positive ``risk`` (reference <= stop)
    corrupts the target math and any downstream 1R risk normalisation. In
    either degenerate case there is no tradable zone, so return None.

    The continuation target is structure-aware (Step A), a cascade anchored on
    ``reference`` (== the entry ``ceiling``, the price the order fills at):
      1. the nearest standard-candle swing-high resistance strictly above the
         zone ceiling, within ``recent_highs`` (when provided);
      2. else a measured-move fallback ``reference + target_atr_mult * atr``;
      3. floored at ``reference + min_target_r * risk`` so the R:R is never thin.
    ``recent_highs`` are the STANDARD candle highs (oldest->newest); when omitted
    the structure step is skipped and the cascade starts at the ATR fallback.
    """
    floor = swing_low + cfg.floor_buffer_atr * atr
    ceiling = trigger_close + cfg.ceiling_atr_mult * atr
    stop = swing_low - cfg.stop_buffer_atr * atr
    # R is measured from the price the order actually fills at -- the entry CEILING (the
    # buy-at-or-below limit). Sizing (insight.size_order), the shadow-book fill
    # (fill.resolve_fill) and live actionability all anchor 1R on ``ceiling - stop``;
    # anchoring the target floor here too makes the advertised ``min_target_r`` the R:R you
    # actually realize, not an R measured from a midpoint you never trade.
    reference = ceiling
    risk = reference - stop
    if floor >= ceiling or risk <= 0:
        return None

    resistance = (
        nearest_resistance(recent_highs, ceiling, cfg.target_pivot_width)
        if recent_highs is not None
        else None
    )
    if resistance is not None:
        target = resistance
    else:
        target = reference + cfg.target_atr_mult * atr

    min_target = reference + cfg.min_target_r * risk
    if (target - reference) / risk < cfg.min_target_r:
        target = min_target

    return EntryZone(floor=floor, ceiling=ceiling, stop=stop,
                     target=target, risk=risk, reference=reference)

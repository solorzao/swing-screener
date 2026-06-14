from dataclasses import dataclass

from swing_screener.config import StrategyConfig


@dataclass(frozen=True)
class EntryZone:
    floor: float
    ceiling: float
    stop: float
    target: float
    risk: float       # reference_entry - stop (per share)
    reference: float  # midpoint used for R math


def compute_zone(trigger_close: float, atr: float, swing_low: float,
                 cfg: StrategyConfig) -> EntryZone | None:
    """Compute the entry zone, or None when the zone is degenerate.

    A shallow pullback (swing_low at or above trigger_close) combined with a
    large ATR can push the floor above the ceiling, producing an inverted zone
    where ``floor >= ceiling``. An inverted zone would make resolve_fill
    mis-classify in-zone bars, and a non-positive ``risk`` (reference <= stop)
    corrupts the target math and any downstream 1R risk normalisation. In
    either degenerate case there is no tradable zone, so return None.
    """
    floor = swing_low + cfg.floor_buffer_atr * atr
    ceiling = trigger_close + cfg.ceiling_atr_mult * atr
    stop = swing_low - cfg.stop_buffer_atr * atr
    reference = (floor + ceiling) / 2.0
    risk = reference - stop
    if floor >= ceiling or risk <= 0:
        return None
    target = reference + cfg.target_r_multiple * risk
    return EntryZone(floor=floor, ceiling=ceiling, stop=stop,
                     target=target, risk=risk, reference=reference)

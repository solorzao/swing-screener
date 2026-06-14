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
                 cfg: StrategyConfig) -> EntryZone:
    floor = swing_low + cfg.floor_buffer_atr * atr
    ceiling = trigger_close + cfg.ceiling_atr_mult * atr
    stop = swing_low - cfg.stop_buffer_atr * atr
    reference = (floor + ceiling) / 2.0
    risk = reference - stop
    target = reference + cfg.target_r_multiple * risk
    return EntryZone(floor=floor, ceiling=ceiling, stop=stop,
                     target=target, risk=risk, reference=reference)

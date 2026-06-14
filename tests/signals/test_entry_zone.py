from swing_screener.config import StrategyConfig
from swing_screener.signals.entry_zone import compute_zone


def test_zone_ordering_and_risk():
    cfg = StrategyConfig()
    z = compute_zone(trigger_close=100.0, atr=4.0, swing_low=96.0, cfg=cfg)
    assert z is not None
    assert z.floor < z.ceiling
    assert z.stop < z.floor
    assert z.ceiling == 100.0 + cfg.ceiling_atr_mult * 4.0
    assert z.target > z.ceiling
    # target sits target_r_multiple * risk above the reference entry
    assert z.risk > 0


def test_inverted_zone_returns_none():
    # Shallow pullback (swing_low above trigger_close) + large ATR pushes the
    # floor above the ceiling: floor = 101.2 > ceiling = 100.7 at defaults.
    cfg = StrategyConfig()
    z = compute_zone(trigger_close=100.0, atr=2.0, swing_low=101.0, cfg=cfg)
    assert z is None


def test_non_positive_risk_returns_none():
    # Stop placed above the zone reference yields risk <= 0 while floor < ceiling,
    # isolating the risk guard from the inversion guard.
    cfg = StrategyConfig(stop_buffer_atr=-0.5)
    z = compute_zone(trigger_close=104.0, atr=10.0, swing_low=100.0, cfg=cfg)
    assert z is None

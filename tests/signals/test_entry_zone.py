from swing_screener.config import StrategyConfig
from swing_screener.signals.entry_zone import compute_zone


def test_zone_ordering_and_risk():
    cfg = StrategyConfig()
    z = compute_zone(trigger_close=100.0, atr=4.0, swing_low=96.0, cfg=cfg)
    assert z.floor < z.ceiling
    assert z.stop < z.floor
    assert z.ceiling == 100.0 + cfg.ceiling_atr_mult * 4.0
    assert z.target > z.ceiling
    # target sits target_r_multiple * risk above the reference entry
    assert z.risk > 0

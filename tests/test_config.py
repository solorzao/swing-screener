from swing_screener.config import StrategyConfig


def test_defaults_present():
    cfg = StrategyConfig()
    assert cfg.ema_fast == 20
    assert cfg.ema_slow == 50
    assert cfg.max_pullback_bars >= 1
    assert 0 < cfg.zone_body_frac < 1
    assert cfg.max_hold_bars["4h"] > 0


def test_override():
    cfg = StrategyConfig(ema_fast=10)
    assert cfg.ema_fast == 10

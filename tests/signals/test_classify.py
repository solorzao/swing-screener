from swing_screener.config import StrategyConfig
from swing_screener.indicators.heiken_ashi import heiken_ashi
from swing_screener.signals.classify import classify_ha


def test_bullish_shaved_bottom(bars):
    # green candle, open == low (no lower wick).
    # A seed bar precedes it so the candle under test is not the HA seed bar,
    # whose ha_open is forced to (open+close)/2 and can never sit on the low.
    df = bars([
        {"open": 10, "high": 11, "low": 9, "close": 10},    # seed
        {"open": 10, "high": 13, "low": 10, "close": 12},   # strong up, open near low
    ])
    c = classify_ha(heiken_ashi(df), StrategyConfig())
    assert bool(c["bullish"].iloc[1])
    assert bool(c["shaved_bottom"].iloc[1])


def test_bearish_shaved_head(bars):
    df = bars([
        {"open": 12, "high": 13, "low": 11, "close": 12},   # seed
        {"open": 12, "high": 12, "low": 8, "close": 9},      # strong down, high near open
    ])
    c = classify_ha(heiken_ashi(df), StrategyConfig())
    assert bool(c["bearish"].iloc[1])
    assert bool(c["shaved_head"].iloc[1])


def test_zone_doji_small_body(bars):
    df = bars([{"open": 10, "high": 11, "low": 9, "close": 10.05}])
    c = classify_ha(heiken_ashi(df), StrategyConfig())
    assert bool(c["zone"].iloc[0])

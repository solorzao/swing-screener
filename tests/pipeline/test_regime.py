from swing_screener.config import StrategyConfig
from swing_screener.pipeline.regime import MarketRegime, classify_regime
from tests.conftest import make_bars

CFG = StrategyConfig()


def _spy(n=220, *, step, half_range, start=100.0):
    """Synthetic SPY daily: rising/falling by `step`, with `half_range` candle wings to
    drive ATR%. `step > 0` trends up (bull), `< 0` down (bear)."""
    rows, p = [], start
    for _ in range(n):
        rows.append({"open": p, "high": p + half_range, "low": p - half_range, "close": p + step})
        p += step
    return make_bars(rows)


def test_uptrend_above_200dma_is_bull():
    r = classify_regime(_spy(step=0.1, half_range=0.1), CFG)
    assert r.trend == "bull" and r.known


def test_downtrend_below_200dma_is_bear():
    r = classify_regime(_spy(step=-0.1, half_range=0.1), CFG)
    assert r.trend == "bear"


def test_volatility_buckets():
    # half_range drives ATR%; price ~100 so 2*half_range ≈ ATR ≈ ATR%*100
    assert classify_regime(_spy(step=0.05, half_range=0.1), CFG).vol == "calm"       # ~0.2%
    assert classify_regime(_spy(step=0.05, half_range=0.7), CFG).vol == "elevated"   # ~1.4%
    assert classify_regime(_spy(step=0.05, half_range=1.5), CFG).vol == "high"       # ~3%


def test_too_few_bars_is_unknown():
    r = classify_regime(_spy(n=150, step=0.1, half_range=0.1), CFG)
    assert r == MarketRegime(None, None) and not r.known


def test_none_frame_is_unknown():
    assert classify_regime(None, CFG) == MarketRegime(None, None)

import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.analyze import (
    _avg_dollar_volume,
    _quality_tier,
    _volatility_tier,
    analyze_ticker,
)


def test_quality_tier_thresholds_locked():
    cfg = StrategyConfig()
    assert _quality_tier(4.99, 1e12, cfg) == "penny"      # price < 5
    assert _quality_tier(50.0, 4.9e6, cfg) == "speculative"
    assert _quality_tier(50.0, 5e6, cfg) == "mid"         # 5e6 not < 5e6
    assert _quality_tier(50.0, 49e6, cfg) == "mid"
    assert _quality_tier(50.0, 50e6, cfg) == "reputable"  # 50e6 not < 50e6


def test_volatility_tier_thresholds_locked():
    cfg = StrategyConfig()
    assert _volatility_tier(0.019, cfg) == "low"
    assert _volatility_tier(0.02, cfg) == "med"           # 0.02 not < 0.02
    assert _volatility_tier(0.049, cfg) == "med"
    assert _volatility_tier(0.05, cfg) == "high"          # 0.05 not < 0.05


def test_avg_dollar_volume_nan_window_fails_safe():
    cfg = StrategyConfig()
    frame = pd.DataFrame({"close": [10.0] * 5, "volume": [float("nan")] * 5})
    adv = _avg_dollar_volume(frame, cfg)
    assert adv == 0.0  # non-finite -> 0, never silently "reputable"
    assert _quality_tier(10.0, adv, cfg) == "speculative"


def _firing(bars):
    # uptrend -> shaved-head pullback -> decisive green trigger (HA flips bullish)
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})
    return bars(rows)


def _uptrend(bars):
    return bars([{"open": 10 + i, "high": 11 + i, "low": 9 + i, "close": 10.5 + i}
                 for i in range(60)])


# The synthetic _firing helper uses an outsized one-bar green flip on a steep ramp,
# so its trigger sits ~5 ATR above the fast EMA -- far past the freshness gate. These
# plumbing tests (zone/score/tags/MTF) disable the gate so they exercise their actual
# concern independent of the anti-chase policy (which has its own test below).
_NO_EXT_GATE = StrategyConfig(max_extension_atr=0.0)


def test_fires_with_zone_score_and_tags(bars):
    res = analyze_ticker("AAPL", {"1d": _firing(bars)}, _NO_EXT_GATE)
    assert len(res) == 1
    r = res[0]
    assert r.ticker == "AAPL" and r.timeframe == "1d" and r.horizon == "medium"
    assert r.entry_floor < r.entry_ceiling and r.stop < r.entry_floor
    assert 0.0 <= r.score <= 1.0
    assert r.mtf_aligned is False           # no higher tf supplied
    assert r.quality_tier in {"penny", "speculative", "mid", "reputable"}
    assert r.volatility_tier in {"low", "med", "high"}


def test_mtf_alignment_boosts_score(bars):
    base = analyze_ticker("AAPL", {"1d": _firing(bars)}, _NO_EXT_GATE)[0]
    aligned = analyze_ticker(
        "AAPL", {"1d": _firing(bars), "1wk": _uptrend(bars)}, _NO_EXT_GATE
    )[0]
    assert aligned.mtf_aligned is True
    assert aligned.score > base.score


def test_no_fire_returns_empty(bars):
    res = analyze_ticker("AAPL", {"1d": _uptrend(bars)}, StrategyConfig())
    assert res == []


def test_overextended_trigger_is_filtered_by_freshness_gate(bars):
    # The _firing trigger has already run well past the fast EMA (a chase). With the
    # default gate it is suppressed; disabling the gate surfaces it again. This is the
    # "stop suggesting plays that already ran" rule at the screen layer.
    frames = {"1d": _firing(bars)}
    assert analyze_ticker("AAPL", frames, StrategyConfig()) == []          # gated out
    surfaced = analyze_ticker("AAPL", frames, _NO_EXT_GATE)                # gate off
    assert len(surfaced) == 1
    assert surfaced[0].ctx.extension_atr > StrategyConfig().max_extension_atr

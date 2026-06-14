from swing_screener.config import StrategyConfig
from swing_screener.signals.frame import build_frame
from swing_screener.signals.detect import detect_last_bar


def _uptrend_then_pullback_then_trigger(bars):
    rows = []
    # 1) long, clean uptrend (rising closes) to push ema_fast > ema_slow
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    # 2) shaved-head pullback: 3 down bars, high near open, holding above ema_slow
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    # 3) trigger: decisively strong green that flips HA bullish in one bar
    #    (HA lags, so the green must overcome the pullback-elevated ha_open)
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})
    return bars(rows)


def test_detects_valid_trigger(bars):
    df = _uptrend_then_pullback_then_trigger(bars)
    ctx = detect_last_bar(build_frame(df, StrategyConfig()), StrategyConfig())
    assert ctx is not None
    assert ctx.pullback_bars >= 1
    assert ctx.swing_low < ctx.trigger_close


def test_no_signal_during_uptrend_run(bars):
    rows = [{"open": 10 + i, "high": 11 + i, "low": 10 + i, "close": 11 + i} for i in range(60)]
    ctx = detect_last_bar(build_frame(bars(rows), StrategyConfig()), StrategyConfig())
    assert ctx is None  # no pullback preceding the last green bar


def test_no_signal_when_below_ema_slow(bars):
    rows = [{"open": 50 - i, "high": 51 - i, "low": 49 - i, "close": 50 - i} for i in range(60)]
    rows.append({"open": rows[-1]["close"], "high": rows[-1]["close"] + 2,
                 "low": rows[-1]["close"], "close": rows[-1]["close"] + 1.8})
    ctx = detect_last_bar(build_frame(bars(rows), StrategyConfig()), StrategyConfig())
    assert ctx is None  # downtrend: close not above ema_slow

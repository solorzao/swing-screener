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


def test_no_signal_when_pullback_has_no_shaved_head(bars):
    # uptrend, then a pullback whose red bars have LARGE upper wicks (not shaved
    # heads), then a strong green trigger. The shaved-head gate must reject it.
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(3):
        # bearish (close < open) but high well above the body => big upper wick
        rows.append({"open": p, "high": p + 2.5, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})
    ctx = detect_last_bar(build_frame(bars(rows), StrategyConfig()), StrategyConfig())
    assert ctx is None  # pullback present but no shaved head -> rejected


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


# --- experiment: outside-bar trigger ("outside_bar") -------------------------
# A raw-candle bullish outside (engulfing) bar replaces the smoothed HA flip:
# the trigger must break BOTH the prior bar's high and low AND close up.

def _uptrend_pullback(p_start: float = 10.0) -> list[dict]:
    """A clean uptrend followed by a 3-bar shaved-head pullback. The LAST row is the
    final pullback bar; callers append their own trigger bar."""
    rows = []
    p = p_start
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    return rows


def test_outside_bar_requires_bullish_close(bars):
    # Identical engulfing range; only the close direction differs. The bullish outside
    # bar fires; the same range closing DOWN must be rejected.
    def frame_for(close_up: bool):
        rows = _uptrend_pullback()
        prev = rows[-1]
        lo, hi = prev["low"] - 0.5, prev["high"] + 1.0   # engulfs prev's range on both sides
        if close_up:
            rows.append({"open": lo + 0.2, "high": hi, "low": lo, "close": hi - 0.2})
        else:
            rows.append({"open": hi - 0.2, "high": hi, "low": lo, "close": lo + 0.2})
        return build_frame(bars(rows), StrategyConfig())

    cfg = StrategyConfig(trigger_kind="outside_bar")
    assert detect_last_bar(frame_for(close_up=True), cfg) is not None
    assert detect_last_bar(frame_for(close_up=False), cfg) is None


def test_outside_bar_rejects_non_engulfing_trigger(bars):
    # The default fixture's trigger is a strong green HA flip whose low does NOT break the
    # prior bar's low -> it is not an outside bar. The HA-flip trigger fires; outside_bar must not.
    frame = build_frame(_uptrend_then_pullback_then_trigger(bars), StrategyConfig())
    assert detect_last_bar(frame, StrategyConfig()) is not None
    assert detect_last_bar(frame, StrategyConfig(trigger_kind="outside_bar")) is None


# --- experiment: entry-depth gate ("require_band_touch") ---------------------
# Require the pullback to have actually reached into the EMA20-EMA50 band
# (swing_low <= ema_fast), not merely stayed above ema_slow.

def test_band_touch_rejects_shallow_pullback(bars):
    # The default fixture's shallow 3-bar pullback holds above the fast EMA: the incumbent
    # fires, but require_band_touch must reject it because price never reached the band.
    frame = build_frame(_uptrend_then_pullback_then_trigger(bars), StrategyConfig())
    base_ctx = detect_last_bar(frame, StrategyConfig())
    assert base_ctx is not None
    assert base_ctx.swing_low > float(frame.iloc[-1]["ema_fast"])   # precondition: never reached the band
    assert detect_last_bar(frame, StrategyConfig(require_band_touch=True)) is None


def test_band_touch_allows_pullback_into_band(bars):
    # A deeper pullback that dips into the EMA20-EMA50 band still fires with the gate on.
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(4):  # deeper: 4 bars of ~2.0 drops reach down into the band
        rows.append({"open": p, "high": p + 0.05, "low": p - 2.3, "close": p - 2.0})
        p -= 2.0
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})   # strong green trigger
    frame = build_frame(bars(rows), StrategyConfig())

    base_ctx = detect_last_bar(frame, StrategyConfig())
    assert base_ctx is not None
    last = frame.iloc[-1]
    assert float(last["ema_slow"]) < base_ctx.swing_low <= float(last["ema_fast"])  # in the band
    assert detect_last_bar(frame, StrategyConfig(require_band_touch=True)) is not None


def _flip_then_pause_then_breakout(bars):
    """A valid legacy setup whose flip is followed by a green PAUSE bar (close below the
    flip high), then a close above the flip high -- the cont_confirm_window shape."""
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    flip_close = p + 5.6
    flip_high = p + 6.0
    rows.append({"open": p, "high": flip_high, "low": p, "close": flip_close})   # the flip
    rows.append({"open": flip_close, "high": flip_high - 0.1, "low": flip_close - 0.2,
                 "close": flip_close + 0.2})                                     # green pause
    rows.append({"open": flip_close + 0.2, "high": flip_high + 1.5,
                 "low": flip_close, "close": flip_high + 1.0})                   # breakout
    return bars(rows), flip_high


def test_cont_confirm_window_fires_on_first_close_above_the_flip_high(bars):
    from dataclasses import replace

    df, flip_high = _flip_then_pause_then_breakout(bars)
    frame = build_frame(df, StrategyConfig())
    # legacy trigger: the last bar is not a flip out of a pullback -> no signal
    assert detect_last_bar(frame, StrategyConfig()) is None
    # the breakout came 2 bars after the flip: window=1 is too narrow, window=2 fires
    assert detect_last_bar(frame, replace(StrategyConfig(), cont_confirm_window=1)) is None
    ctx = detect_last_bar(frame, replace(StrategyConfig(), cont_confirm_window=2))
    assert ctx is not None
    assert ctx.trigger_close > flip_high        # priced at the CONFIRMATION bar
    assert ctx.pullback_bars >= 1               # structure validated as-of the flip
    assert ctx.swing_low < flip_high


def test_cont_confirm_window_fires_exactly_once(bars):
    from dataclasses import replace

    import pandas as pd

    df, flip_high = _flip_then_pause_then_breakout(bars)
    last = float(df.iloc[-1]["close"])
    extra = pd.DataFrame(
        [{"open": last, "high": last + 1.5, "low": last - 0.2, "close": last + 1.0,
          "volume": 1_000_000.0}],
        index=[df.index[-1] + pd.Timedelta(days=1)],
    )
    frame = build_frame(pd.concat([df, extra]).astype(float), StrategyConfig())
    # the day AFTER the first close above the flip high: an earlier run bar already
    # confirmed -> the trigger must not re-fire (window=3 so the run length isn't the
    # thing rejecting it)
    assert detect_last_bar(frame, replace(StrategyConfig(), cont_confirm_window=3)) is None


def test_cont_confirm_window_nan_atr_on_confirmation_bar_never_fires(bars):
    # the recursion validates the FLIP bar, but atr/rsi are read from TODAY's bar:
    # a NaN there used to ship a context with atr=NaN (NaN is truthy, so the
    # `if atr` extension fallback never engaged). NaN indicator -> no signal.
    from dataclasses import replace

    df, _ = _flip_then_pause_then_breakout(bars)
    frame = build_frame(df, StrategyConfig()).copy()
    frame.iloc[-1, frame.columns.get_loc("atr")] = float("nan")
    assert detect_last_bar(frame, replace(StrategyConfig(), cont_confirm_window=2)) is None


def test_cont_confirm_window_never_fires_on_the_flip_bar_itself(bars):
    from dataclasses import replace

    df = _uptrend_then_pullback_then_trigger(bars)  # ends ON the flip bar
    frame = build_frame(df, StrategyConfig())
    assert detect_last_bar(frame, StrategyConfig()) is not None          # legacy fires
    # the windowed variant is a DISJOINT entry-timing cohort: it waits for confirmation
    assert detect_last_bar(frame, replace(StrategyConfig(), cont_confirm_window=2)) is None

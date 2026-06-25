from dataclasses import dataclass

import pandas as pd

from swing_screener.config import StrategyConfig


@dataclass(frozen=True)
class PullbackContext:
    trigger_ts: pd.Timestamp
    trigger_close: float
    atr: float
    swing_low: float          # lowest low across the pullback window
    pullback_bars: int
    shaved_bottom: bool       # trigger quality flag
    rsi: float
    # how far the trigger close already sits above the fast EMA, in ATR units.
    # A freshness/anti-chase measure: a large value means the move "already ran".
    # Defaulted so callers/tests that construct a context by hand stay valid.
    extension_atr: float = 0.0


def detect_last_bar(f: pd.DataFrame, cfg: StrategyConfig) -> PullbackContext | None:
    """Return a PullbackContext if the last closed bar of ``f`` is a valid
    pullback-continuation long trigger, else None.

    ``f`` is the enriched frame from ``build_frame`` (HA + EMAs + ATR + RSI +
    classification). Pure: reads only, never mutates ``f``.
    """
    if len(f) < cfg.ema_slow + cfg.max_pullback_bars + 2:
        return None

    last = f.iloc[-1]
    prev = f.iloc[-2]
    # 1) uptrend context at the trigger bar
    if not (last["ema_fast"] > last["ema_slow"] and last["close"] > last["ema_slow"]):
        return None
    # trigger: the incumbent bullish HA flip, or (experiment) a raw bullish outside bar
    # whose range engulfs the prior bar on BOTH sides and closes up.
    if cfg.trigger_kind == "outside_bar":
        is_outside = last["high"] > prev["high"] and last["low"] < prev["low"]
        if not (is_outside and last["close"] > last["open"]):
            return None
    elif not bool(last["bullish"]):
        return None

    # 2) walk back over the immediately preceding bars looking for the pullback
    #    (contiguous bearish/zone bars), and require a bearish shaved head within it.
    pullback = []
    saw_shaved_head = False
    for k in range(2, cfg.max_pullback_bars + 2):
        bar = f.iloc[-k]
        if bool(bar["bearish"]) or bool(bar["zone"]):
            pullback.append(bar)
            if bool(bar["shaved_head"]):
                saw_shaved_head = True
        else:
            break

    if len(pullback) < cfg.min_pullback_bars or not saw_shaved_head:
        return None

    # use the real traded low (not the smoothed HA low) so stops/zones in Task 7
    # sit off actual price, and the shallow-pullback gate reflects true price.
    swing_low = min(b["low"] for b in pullback)
    # 3) shallow pullback: stayed above ema_slow (continuation, not reversal)
    if swing_low <= last["ema_slow"]:
        return None
    # 3b) entry-depth gate (experiment): require the pullback to have actually reached into
    # the EMA20-EMA50 band, i.e. price pulled back to value rather than barely dipping while
    # still extended above the fast EMA.
    if cfg.require_band_touch and swing_low > last["ema_fast"]:
        return None

    atr = float(last["atr"])
    # Extension above the fast EMA in ATR units -- the anti-chase/freshness measure.
    # The gate itself lives in analyze_frames (policy); detect only reports the metric.
    extension_atr = (float(last["close"]) - float(last["ema_fast"])) / atr if atr else 0.0

    return PullbackContext(
        trigger_ts=f.index[-1],
        trigger_close=float(last["close"]),
        atr=atr,
        swing_low=float(swing_low),
        pullback_bars=len(pullback),
        shaved_bottom=bool(last["shaved_bottom"]),
        rsi=float(last["rsi"]),
        extension_atr=extension_atr,
    )

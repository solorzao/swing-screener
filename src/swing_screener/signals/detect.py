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


def detect_last_bar(f: pd.DataFrame, cfg: StrategyConfig) -> PullbackContext | None:
    """Return a PullbackContext if the last closed bar of ``f`` is a valid
    pullback-continuation long trigger, else None.

    ``f`` is the enriched frame from ``build_frame`` (HA + EMAs + ATR + RSI +
    classification). Pure: reads only, never mutates ``f``.
    """
    if len(f) < cfg.ema_slow + cfg.max_pullback_bars + 2:
        return None

    last = f.iloc[-1]
    # 1) uptrend context at the trigger bar
    if not (last["ema_fast"] > last["ema_slow"] and last["close"] > last["ema_slow"]):
        return None
    # trigger must be a bullish HA candle
    if not bool(last["bullish"]):
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

    swing_low = min(b["ha_low"] for b in pullback)
    # 3) shallow pullback: stayed above ema_slow (continuation, not reversal)
    if swing_low <= last["ema_slow"]:
        return None

    return PullbackContext(
        trigger_ts=f.index[-1],
        trigger_close=float(last["close"]),
        atr=float(last["atr"]),
        swing_low=float(swing_low),
        pullback_bars=len(pullback),
        shaved_bottom=bool(last["shaved_bottom"]),
        rsi=float(last["rsi"]),
    )

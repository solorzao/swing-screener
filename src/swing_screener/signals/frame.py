import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.indicators.heiken_ashi import heiken_ashi
from swing_screener.indicators.trend import atr, ema, rsi
from swing_screener.signals.classify import classify_ha


def build_frame(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Assemble raw OHLCV + HA + EMAs + ATR + RSI + classification into one frame.

    Pure w.r.t. ``df``: the input is not mutated.
    """
    ha = heiken_ashi(df)
    cls = classify_ha(ha, cfg)
    out = df.join(ha)
    out["ema_fast"] = ema(df["close"], cfg.ema_fast)
    out["ema_slow"] = ema(df["close"], cfg.ema_slow)
    out["atr"] = atr(df, cfg.atr_period)
    out["rsi"] = rsi(df["close"], cfg.rsi_period)
    return out.join(cls)

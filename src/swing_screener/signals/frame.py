import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.indicators.heiken_ashi import heiken_ashi
from swing_screener.indicators.trend import atr, ema, macd_histogram, rsi
from swing_screener.signals.classify import classify_ha


def build_frame(df: pd.DataFrame, cfg: StrategyConfig, *,
                spy_close: pd.Series | None = None) -> pd.DataFrame:
    """Assemble raw OHLCV + HA + EMAs + ATR + RSI + classification into one frame.

    Pure w.r.t. ``df``: the input is not mutated. When ``spy_close`` (the market-proxy daily
    close) is given, add a relative-strength column ``rs = close / spy_close`` (SPY aligned to
    ``df``'s dates; NaN where SPY is missing) -- the basis for the RS-leadership gate. Omit it
    and no ``rs`` column is added (back-compat; the gate then no-ops).
    """
    ha = heiken_ashi(df)
    cls = classify_ha(ha, cfg)
    out = df.join(ha)
    out["ema_fast"] = ema(df["close"], cfg.ema_fast)
    out["ema_slow"] = ema(df["close"], cfg.ema_slow)
    out["atr"] = atr(df, cfg.atr_period)
    out["rsi"] = rsi(df["close"], cfg.rsi_period)
    out["macd_hist"] = macd_histogram(df["close"], cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    out["macd_hist_rising"] = out["macd_hist"] > out["macd_hist"].shift(1)
    if spy_close is not None:
        out["rs"] = df["close"] / spy_close.reindex(df.index)
    return out.join(cls)

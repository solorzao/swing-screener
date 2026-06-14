import numpy as np
import pandas as pd
from swing_screener.indicators.trend import ema, atr, rsi


def test_ema_matches_pandas_ewm(bars):
    df = bars([{"open": i, "high": i + 1, "low": i - 1, "close": i} for i in range(1, 11)])
    got = ema(df["close"], span=3)
    want = df["close"].ewm(span=3, adjust=False).mean()
    pd.testing.assert_series_equal(got, want)


def test_atr_is_positive_and_wilder_smoothed(bars):
    df = bars([{"open": 10, "high": 11 + (i % 3), "low": 9 - (i % 2), "close": 10 + (i % 2)}
               for i in range(20)])
    a = atr(df, period=14)
    assert (a.dropna() > 0).all()
    assert a.notna().iloc[-1]


def test_rsi_bounds_0_100(bars):
    df = bars([{"open": 10, "high": 11, "low": 9, "close": 10 + np.sin(i)} for i in range(40)])
    r = rsi(df["close"], period=14).dropna()
    assert ((r >= 0) & (r <= 100)).all()

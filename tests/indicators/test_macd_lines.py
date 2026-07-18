"""The full macd() (line + signal + hist) and its lockstep with macd_histogram --
the lab's MACD pane consumes the lines the old helper computed and discarded."""

import pandas as pd

from swing_screener.indicators.trend import ema, macd, macd_histogram


def _series() -> pd.Series:
    return pd.Series([100 + (i % 7) * 1.5 + i * 0.2 for i in range(120)])


def test_macd_columns_and_math() -> None:
    s = _series()
    out = macd(s, 12, 26, 9)
    assert list(out.columns) == ["macd", "signal", "hist"]
    expected_line = ema(s, 12) - ema(s, 26)
    assert (out["macd"] - expected_line).abs().max() < 1e-12
    assert (out["signal"] - ema(expected_line, 9)).abs().max() < 1e-12
    assert (out["hist"] - (out["macd"] - out["signal"])).abs().max() < 1e-12


def test_macd_histogram_lockstep() -> None:
    """macd_histogram now delegates to macd(); the two must never drift."""
    s = _series()
    assert (macd_histogram(s) - macd(s)["hist"]).abs().max() < 1e-12

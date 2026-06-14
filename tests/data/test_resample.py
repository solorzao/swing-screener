import pandas as pd

from swing_screener.data.resample import resample_ohlcv


def _hourly(n: int) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01 09:00", periods=n, freq="1h")
    return pd.DataFrame(
        {"open": range(n), "high": [i + 2 for i in range(n)],
         "low": [i - 1 for i in range(n)], "close": [i + 1 for i in range(n)],
         "volume": [100] * n},
        index=idx,
    ).astype(float)


def test_1h_to_4h_aggregates_ohlcv():
    df = _hourly(8)  # exactly two 4h buckets
    out = resample_ohlcv(df, "4h")
    assert len(out) == 2
    first = out.iloc[0]
    # bucket 0 = hours 0..3
    assert first["open"] == 0.0          # first open
    assert first["high"] == 5.0          # max high (i+2 over i=0..3 -> 5)
    assert first["low"] == -1.0          # min low
    assert first["close"] == 4.0         # last close (i+1 at i=3)
    assert first["volume"] == 400.0      # sum


def test_daily_to_weekly_aggregates_and_drops_partial_nans():
    idx = pd.date_range("2024-01-01", periods=10, freq="1D")  # Mon-Wed across 2 weeks
    df = pd.DataFrame(
        {"open": range(10), "high": [i + 1 for i in range(10)],
         "low": [i - 1 for i in range(10)], "close": [i for i in range(10)],
         "volume": [10] * 10},
        index=idx,
    ).astype(float)
    out = resample_ohlcv(df, "1W")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert (out["volume"] > 0).all()  # no all-NaN/empty rows survive

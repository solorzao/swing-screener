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


def test_weekly_partial_bar_labeled_by_last_trading_day_not_future_sunday():
    # Mon 2024-01-01 .. Wed 2024-01-10 (10 daily bars). Pandas RIGHT-labels weekly
    # bars at the week-ending Sunday, so the final (partial) week would be stamped
    # 2024-01-14 -- a FUTURE date past the last real bar (2024-01-10). The relabel
    # must date each weekly bar by the LAST underlying daily bar in its bucket.
    idx = pd.date_range("2024-01-01", periods=10, freq="1D")
    df = pd.DataFrame(
        {"open": range(10), "high": [i + 1 for i in range(10)],
         "low": [i - 1 for i in range(10)], "close": [i for i in range(10)],
         "volume": [10] * 10},
        index=idx,
    ).astype(float)
    out = resample_ohlcv(df, "1W")
    last_ts = out.index[-1]
    assert last_ts == idx[-1]  # last real daily bar, 2024-01-10
    assert last_ts == pd.Timestamp("2024-01-10")
    assert last_ts <= idx[-1]  # never a future (week-ending Sunday) date
    # the closed first week ends on its real last trading day, Sun 2024-01-07
    assert out.index[0] == pd.Timestamp("2024-01-07")


def test_monthly_partial_bar_labeled_by_last_trading_day():
    # Jan + a partial Feb. The month-end (ME) rule right-labels Feb at 2024-02-29,
    # but the data stops on 2024-02-05; the bar must be dated by that last bar.
    idx = pd.date_range("2024-01-15", periods=22, freq="1D")  # ends 2024-02-05
    df = pd.DataFrame(
        {"open": range(22), "high": [i + 1 for i in range(22)],
         "low": [i - 1 for i in range(22)], "close": [i for i in range(22)],
         "volume": [10] * 22},
        index=idx,
    ).astype(float)
    out = resample_ohlcv(df, "1ME")
    assert out.index[-1] == idx[-1]  # 2024-02-05, the last real bar
    assert out.index[-1] == pd.Timestamp("2024-02-05")
    # January's bar is dated by its last trading day in-sample, 2024-01-31
    assert out.index[0] == pd.Timestamp("2024-01-31")

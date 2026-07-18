from datetime import date

import pandas as pd
import pytest

from swing_screener.data import fetch


def _df():
    idx = pd.date_range("2024-01-01", periods=3, freq="1D")
    return pd.DataFrame(
        {"open": [1.0, 2, 3], "high": [2.0, 3, 4], "low": [0.5, 1, 2],
         "close": [1.5, 2, 3], "volume": [100.0, 100, 100]}, index=idx,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)


def test_fetch_bars_caches_second_call(tmp_path, monkeypatch):
    calls = {"n": 0}
    def fake_download(ticker, interval, period):
        calls["n"] += 1
        return _df()
    monkeypatch.setattr(fetch, "_download", fake_download)

    a = fetch.fetch_bars("AAPL", "1d", cache_dir=tmp_path)
    b = fetch.fetch_bars("AAPL", "1d", cache_dir=tmp_path)
    assert a is not None and b is not None
    assert len(a) == 3
    assert calls["n"] == 1  # second call served from parquet cache


def test_fetch_bars_returns_none_on_persistent_failure(tmp_path, monkeypatch):
    def boom(ticker, interval, period):
        raise RuntimeError("yahoo down")
    monkeypatch.setattr(fetch, "_download", boom)
    assert fetch.fetch_bars("AAPL", "1d", cache_dir=tmp_path, retries=3) is None


def test_fetch_universe_isolates_failures(tmp_path, monkeypatch):
    def selective(ticker, interval, period):
        if ticker == "BAD":
            raise RuntimeError("nope")
        return _df()
    monkeypatch.setattr(fetch, "_download", selective)
    out = fetch.fetch_universe(["AAPL", "BAD", "MSFT"], "1d", cache_dir=tmp_path)
    assert set(out.keys()) == {"AAPL", "MSFT"}  # BAD skipped, batch survived


def test_fetch_retries_with_jittered_backoff(tmp_path, monkeypatch):
    # transient failures retry with exponential backoff + jitter, then give up;
    # jitter decorrelates the universe's retries so they don't hammer in lockstep.
    sleeps: list[float] = []
    monkeypatch.setattr(fetch.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(fetch.random, "uniform", lambda a, b: b)  # deterministic jitter = b
    attempts = {"n": 0}

    def flaky(ticker, interval, period):
        attempts["n"] += 1
        raise RuntimeError("rate limited")
    monkeypatch.setattr(fetch, "_download", flaky)

    out = fetch.fetch_bars("AAPL", "1d", cache_dir=tmp_path, retries=3, backoff=0.5, jitter=0.5)
    assert out is None
    assert attempts["n"] == 3  # all attempts made
    # slept only BETWEEN attempts (2 sleeps for 3 attempts), each backoff + jitter
    assert sleeps == [0.5 * 1 + 0.5, 0.5 * 2 + 0.5]


def test_corrupt_cache_falls_through_to_download(tmp_path, monkeypatch):
    # a corrupt/partial cache file must not break isolation: re-download instead
    cache_file = fetch._cache_path(tmp_path, "1d", "AAPL", date.today())
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("not a real parquet file")
    monkeypatch.setattr(fetch, "_download", lambda *a, **k: _df())

    out = fetch.fetch_bars("AAPL", "1d", cache_dir=tmp_path)
    assert out is not None and len(out) == 3  # recovered via re-download
    # and the cache was overwritten with a valid parquet (next read succeeds)
    assert pd.read_parquet(cache_file).shape[0] == 3


def _yf_frame_with_nan_close(rows: int = 30, nan_at: int = 10) -> pd.DataFrame:
    """yfinance-shaped frame (capitalized columns) with one NaN close mid-frame --
    the 2026-07-05 NaN-holiday-close failure class."""
    idx = pd.date_range("2024-01-01", periods=rows, freq="1D")
    df = pd.DataFrame(
        {"Open": [1.0 + i for i in range(rows)], "High": [2.0 + i for i in range(rows)],
         "Low": [0.5 + i for i in range(rows)], "Close": [1.5 + i for i in range(rows)],
         "Adj Close": [1.5 + i for i in range(rows)], "Volume": [100.0] * rows}, index=idx,
    )
    df.iloc[nan_at, df.columns.get_loc("Close")] = float("nan")
    return df


def test_download_drops_nan_ohlc_rows(monkeypatch):
    """One NaN close must be dropped at the seam: it would otherwise poison the
    Heiken-Ashi open recursion for every subsequent bar (NaN never leaves the
    ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2 chain), silently killing all
    detectors for the ticker forever. Fixed here so every consumer is covered."""
    frame = _yf_frame_with_nan_close(rows=30, nan_at=10)
    monkeypatch.setattr(fetch.yf, "download", lambda *a, **k: frame)
    out = fetch._download("TEST", "1d", "5y")
    assert not out[["open", "high", "low", "close"]].isna().any().any()
    assert len(out) == 29  # exactly the NaN row is gone
    assert frame.index[10] not in out.index
    # neighbors intact: dropping is per-row, never a truncation
    assert out.loc[frame.index[9], "close"] == 1.5 + 9
    assert out.loc[frame.index[11], "close"] == 1.5 + 11


def test_download_keeps_rows_with_nan_volume(monkeypatch):
    """Volume is deliberately NOT in the dropna subset: index tickers (^VIX) have
    no real volume, and the HA recursion only reads OHLC -- dropping on volume
    could wipe an otherwise-healthy frame."""
    frame = _yf_frame_with_nan_close(rows=5, nan_at=2)
    frame["Volume"] = float("nan")
    monkeypatch.setattr(fetch.yf, "download", lambda *a, **k: frame)
    out = fetch._download("^VIX", "1d", "2y")
    assert len(out) == 4  # only the NaN-close row dropped; NaN volume survives


def test_fetch_bars_returns_none_when_every_row_is_nan(tmp_path, monkeypatch):
    """An all-NaN download now becomes an empty frame at the seam, which trips
    fetch_bars' 'empty frame' check -> None (per-ticker isolation). That is the
    correct fail-safe: no cache write, no poisoned frame handed downstream."""
    frame = _yf_frame_with_nan_close(rows=3, nan_at=0)
    frame[["Open", "High", "Low", "Close"]] = float("nan")
    monkeypatch.setattr(fetch.yf, "download", lambda *a, **k: frame)
    assert fetch.fetch_bars("DEAD", "1d", cache_dir=tmp_path, retries=2) is None
    assert not (tmp_path / "1d").exists()  # nothing cached


def test_avg_dollar_volume_means_close_times_volume():
    from swing_screener.data.fetch import avg_dollar_volume
    df = pd.DataFrame({"close": [10.0, 20.0], "volume": [100.0, 100.0]})
    assert avg_dollar_volume(df) == 1500.0           # (10*100 + 20*100)/2
    assert avg_dollar_volume(df, window=1) == 2000.0  # last bar only
    assert avg_dollar_volume(pd.DataFrame({"close": [], "volume": []})) is None


def test_fetch_market_cap_caches(tmp_path, monkeypatch):
    from swing_screener.data import fetch
    calls = {"n": 0}
    def fake(ticker):
        calls["n"] += 1
        return 1234.0
    monkeypatch.setattr(fetch, "_fast_info_market_cap", fake)
    a = fetch.fetch_market_cap("AAPL", cache_dir=tmp_path)
    b = fetch.fetch_market_cap("AAPL", cache_dir=tmp_path)
    assert a == 1234.0 and b == 1234.0
    assert calls["n"] == 1  # second served from cache


def test_fetch_market_cap_none_on_failure(tmp_path, monkeypatch):
    from swing_screener.data import fetch
    monkeypatch.setattr(fetch, "_fast_info_market_cap",
                        lambda t: (_ for _ in ()).throw(RuntimeError("down")))
    assert fetch.fetch_market_cap("AAPL", cache_dir=tmp_path, retries=2) is None


def test_fetch_market_cap_missing_value_returns_none_no_cache(tmp_path, monkeypatch):
    from swing_screener.data import fetch
    monkeypatch.setattr(fetch, "_fast_info_market_cap", lambda t: None)
    assert fetch.fetch_market_cap("ETF", cache_dir=tmp_path) is None
    assert not (tmp_path / "marketcap").exists()  # a clean None is not cached


def test_fast_info_market_cap_rejects_nan_zero_and_negative(monkeypatch):
    # yfinance fast_info can surface NaN/0 for a missing cap; those must read as None
    # (NaN is truthy, so it would otherwise be cached/persisted). Mapping-style access.
    from swing_screener.data import fetch

    class _FakeTicker:
        def __init__(self, info):
            self.fast_info = info

    for bad in (float("nan"), float("inf"), 0.0, -5.0):
        monkeypatch.setattr(fetch.yf, "Ticker", lambda t, v=bad: _FakeTicker({"market_cap": v}))
        assert fetch._fast_info_market_cap("X") is None
    monkeypatch.setattr(fetch.yf, "Ticker", lambda t: _FakeTicker({"market_cap": 1000.0}))
    assert fetch._fast_info_market_cap("X") == 1000.0


def test_fetch_sector_caches(tmp_path, monkeypatch):
    from swing_screener.data import fetch
    calls = {"n": 0}

    def fake(t):
        calls["n"] += 1
        return "Information Technology"

    monkeypatch.setattr(fetch, "_info_sector", fake)
    a = fetch.fetch_sector("AAPL", cache_dir=tmp_path)
    b = fetch.fetch_sector("AAPL", cache_dir=tmp_path)
    assert a == b == "Information Technology"
    assert calls["n"] == 1  # second call served from the per-day cache


def test_fetch_sector_none_on_failure(tmp_path, monkeypatch):
    from swing_screener.data import fetch
    monkeypatch.setattr(fetch, "_info_sector",
                        lambda t: (_ for _ in ()).throw(RuntimeError("down")))
    assert fetch.fetch_sector("AAPL", cache_dir=tmp_path, retries=2) is None


def test_fetch_sector_missing_value_returns_none_no_cache(tmp_path, monkeypatch):
    from swing_screener.data import fetch
    monkeypatch.setattr(fetch, "_info_sector", lambda t: None)
    assert fetch.fetch_sector("ETF", cache_dir=tmp_path) is None
    assert not (tmp_path / "sector").exists()  # a clean None is not cached


def test_info_sector_reads_info_dict(monkeypatch):
    from swing_screener.data import fetch

    class _FakeTicker:
        def __init__(self, info):
            self.info = info

    monkeypatch.setattr(fetch.yf, "Ticker", lambda t: _FakeTicker({"sector": " Energy "}))
    assert fetch._info_sector("XOM") == "Energy"  # trimmed
    monkeypatch.setattr(fetch.yf, "Ticker", lambda t: _FakeTicker({}))
    assert fetch._info_sector("X") is None  # no sector key


def _df_ending(last_day: str, periods: int = 3):
    idx = pd.date_range(end=last_day, periods=periods, freq="1D")
    return pd.DataFrame(
        {"open": [1.0] * periods, "high": [2.0] * periods, "low": [0.5] * periods,
         "close": [1.5] * periods, "volume": [100.0] * periods}, index=idx,
    )


def test_daily_fetch_drops_todays_in_progress_bar_before_the_close(tmp_path, monkeypatch):
    """The 'daily bar is complete' invariant, enforced at the cache seam: a 2pm dashboard
    fetch must not pin today's half-formed session bar as the day's cached truth (the
    evening screen would detect/fill/advance off it -- 2026-07 audit's partial-bar
    contamination). After the close (or for prior dates) the bar passes through."""
    from datetime import datetime

    monkeypatch.setattr(fetch, "_download", lambda *a: _df_ending("2026-07-03"))

    # 2pm ET on the bar's own date: the in-progress bar is dropped before caching
    monkeypatch.setattr(fetch, "_now_eastern",
                        lambda: datetime(2026, 7, 3, 14, 0, tzinfo=fetch._EASTERN))
    df = fetch.fetch_bars("AAPL", "1d", cache_dir=tmp_path)
    assert df is not None and len(df) == 2
    assert df.index[-1].date() == date(2026, 7, 2)

    # 5pm ET same day (fresh cache dir): the session closed, the bar is kept
    monkeypatch.setattr(fetch, "_now_eastern",
                        lambda: datetime(2026, 7, 3, 17, 0, tzinfo=fetch._EASTERN))
    df = fetch.fetch_bars("AAPL", "1d", cache_dir=tmp_path / "pm")
    assert df is not None and len(df) == 3

    # a later calendar day (fresh cache dir): historical bars are never touched
    monkeypatch.setattr(fetch, "_now_eastern",
                        lambda: datetime(2026, 7, 6, 14, 0, tzinfo=fetch._EASTERN))
    df = fetch.fetch_bars("AAPL", "1d", cache_dir=tmp_path / "later")
    assert df is not None and len(df) == 3


def test_hourly_fetch_is_not_touched_by_the_daily_guard(tmp_path, monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(fetch, "_download", lambda *a: _df_ending("2026-07-03"))
    monkeypatch.setattr(fetch, "_now_eastern",
                        lambda: datetime(2026, 7, 3, 14, 0, tzinfo=fetch._EASTERN))
    df = fetch.fetch_bars("AAPL", "1h", cache_dir=tmp_path, period="60d")
    assert df is not None and len(df) == 3  # intraday frames keep every row

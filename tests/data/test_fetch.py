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

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

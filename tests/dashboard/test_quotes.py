import pandas as pd

from swing_screener.data import fetch
from swing_screener.dashboard import quotes


def _df(last_close: float) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=2, freq="1D")
    return pd.DataFrame(
        {"open": [1.0, 2.0], "high": [2.0, 3.0], "low": [0.5, 1.0],
         "close": [1.0, last_close], "volume": [100.0, 100.0]}, index=idx,
    )


def test_latest_close_returns_last_close(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "fetch_bars", lambda ticker, interval, **kw: _df(42.5))
    assert quotes.latest_close("AAPL", cache_dir=tmp_path) == 42.5


def test_latest_close_none_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "fetch_bars", lambda ticker, interval, **kw: None)
    assert quotes.latest_close("AAPL", cache_dir=tmp_path) is None


def test_latest_closes_skips_misses(tmp_path, monkeypatch):
    def fake(ticker, interval, **kw):
        return _df(10.0) if ticker == "AAPL" else None
    monkeypatch.setattr(fetch, "fetch_bars", fake)
    out = quotes.latest_closes(["AAPL", "BAD"], cache_dir=tmp_path)
    assert out == {"AAPL": 10.0}

"""fetch_lab_frame: per-timeframe fetch/resample glue with the USER-TRIGGERED
error posture (RuntimeError on empty upstream, unlike fetch_bars' None)."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from swing_screener.lab import frames


def _daily(n: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(5)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1,
         "close": close, "volume": [1e6] * n},
        index=pd.bdate_range("2026-01-01", periods=n),
    )


def _hourly(n: int = 140) -> pd.DataFrame:
    rng = np.random.default_rng(6)
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    return pd.DataFrame(
        {"open": close, "high": close + 0.4, "low": close - 0.4,
         "close": close, "volume": [2e5] * n},
        index=pd.date_range("2026-06-01 09:30", periods=n, freq="h"),
    )


def test_daily_passthrough(monkeypatch, tmp_path: Path) -> None:
    daily = _daily()
    monkeypatch.setattr(frames, "fetch_bars", lambda *a, **k: daily)
    out = frames.fetch_lab_frame("AMD", "1d", cache_dir=tmp_path)
    assert out is daily


def test_weekly_and_monthly_resample(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(frames, "fetch_bars", lambda *a, **k: _daily())
    wk = frames.fetch_lab_frame("AMD", "1wk", cache_dir=tmp_path)
    mo = frames.fetch_lab_frame("AMD", "1mo", cache_dir=tmp_path)
    assert len(wk) < 120 and len(mo) < len(wk)
    # Calendar bars are relabeled to the last real trading day, never a future date.
    assert wk.index[-1] <= _daily().index[-1]


def test_4h_resamples_hourly(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple] = []

    def fake_fetch(ticker, interval, **kw):
        calls.append((ticker, interval, kw.get("period")))
        return _hourly()

    monkeypatch.setattr(frames, "fetch_bars", fake_fetch)
    out = frames.fetch_lab_frame("AMD", "4h", cache_dir=tmp_path)
    assert calls == [("AMD", "1h", "60d")]
    assert 0 < len(out) < 140


def test_empty_upstream_raises(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(frames, "fetch_bars", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        frames.fetch_lab_frame("AMD", "1d", cache_dir=tmp_path)
    with pytest.raises(RuntimeError):
        frames.fetch_lab_frame("AMD", "4h", cache_dir=tmp_path)


def test_unknown_timeframe_is_a_caller_bug(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        frames.fetch_lab_frame("AMD", "2h", cache_dir=tmp_path)

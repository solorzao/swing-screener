"""The daily fetch horizon must support EVERY screened timeframe.

The 1mo frame is resampled from the daily series: 2 years of dailies yield ~24 monthly
bars while the detectors need ~56 (ema_slow 50 + pullback 4 + 2), so the 1mo timeframe
could never produce a signal and the monthly digest cadence was permanently empty
(2026-07 audit; the production signals table has ZERO 1mo rows ever). Five years yields
~60 monthly bars -- above the minimum. The cache is keyed by (interval, ticker, day)
with no period component, so the horizon must be a single default, not per-call.
"""

from datetime import date

import numpy as np
import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.data import fetch
from swing_screener.data.resample import resample_ohlcv


def test_daily_fetch_defaults_to_five_years(monkeypatch, tmp_path):
    seen: dict[str, str] = {}

    def fake_download(ticker, interval, period):
        seen["period"] = period
        idx = pd.bdate_range("2021-01-04", periods=10)
        return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                             "volume": 1.0}, index=idx)

    monkeypatch.setattr(fetch, "_download", fake_download)
    fetch.fetch_bars("AMD", "1d", cache_dir=tmp_path, today=date(2026, 7, 2))
    assert seen["period"] == "5y"


def test_five_years_of_dailies_meet_the_monthly_detector_minimum():
    cfg = StrategyConfig()
    minimum = cfg.ema_slow + cfg.max_pullback_bars + 2  # detect_last_bar's floor
    idx = pd.bdate_range(end="2026-07-02", periods=252 * 5)  # ~5y of trading days
    n = len(idx)
    df = pd.DataFrame({"open": np.full(n, 1.0), "high": np.full(n, 1.0),
                       "low": np.full(n, 1.0), "close": np.full(n, 1.0),
                       "volume": np.full(n, 1.0)}, index=idx)
    monthly = resample_ohlcv(df, "1ME")
    assert len(monthly) >= minimum

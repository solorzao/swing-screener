"""Per-timeframe OHLCV for the lab. Thin glue over data.fetch + data.resample,
mirroring pipeline.run._fetch_all_timeframes but per single timeframe (the lab
fetches only the frame the user is looking at) and with the USER-TRIGGERED error
posture: failure RAISES RuntimeError (like options.chain) instead of returning
None -- a lab fetch the user just asked for must fail loudly, not silently.
"""

from datetime import date
from pathlib import Path

import pandas as pd

from swing_screener.data.fetch import fetch_bars
from swing_screener.data.resample import resample_ohlcv

# The four lab timeframes. 4h is resampled from 1h bars, which yfinance caps at
# ~730 days; period='60d' matches the screener's pipeline so the parquet cache is
# shared. Weekly/monthly resample from the 5y daily fetch.
LAB_TIMEFRAMES = ("4h", "1d", "1wk", "1mo")


def fetch_lab_frame(ticker: str, timeframe: str, *, cache_dir: Path,
                    today: date | None = None) -> pd.DataFrame:
    """OHLCV frame for one lab timeframe. Raises RuntimeError when the upstream
    fetch comes back empty (the router maps that to a 503), ValueError on an
    unknown timeframe (a caller bug -- the API validates before calling)."""
    if timeframe not in LAB_TIMEFRAMES:
        raise ValueError(f"unknown lab timeframe {timeframe!r}")
    if timeframe == "4h":
        hourly = fetch_bars(ticker, "1h", cache_dir=cache_dir, today=today,
                            period="60d")
        if hourly is None or hourly.empty:
            raise RuntimeError(f"no hourly bars for {ticker}")
        return resample_ohlcv(hourly, "4h")
    daily = fetch_bars(ticker, "1d", cache_dir=cache_dir, today=today)
    if daily is None or daily.empty:
        raise RuntimeError(f"no daily bars for {ticker}")
    if timeframe == "1d":
        return daily
    if timeframe == "1wk":
        return resample_ohlcv(daily, "1W")
    return resample_ohlcv(daily, "1ME")

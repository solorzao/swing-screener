import importlib.util
import logging
import time
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

_COLS = ["open", "high", "low", "close", "volume"]


def _has_parquet_engine() -> bool:
    """True if pandas can read/write parquet (pyarrow or fastparquet installed)."""
    return any(importlib.util.find_spec(m) is not None for m in ("pyarrow", "fastparquet"))


def _cache_path(cache_dir: Path, interval: str, ticker: str, today: date) -> Path:
    """On-disk cache location. Uses parquet when an engine is available, else falls
    back to pickle so the cache still works in environments without pyarrow."""
    ext = "parquet" if _has_parquet_engine() else "pkl"
    return Path(cache_dir) / interval / f"{ticker}_{today:%Y%m%d}.{ext}"


def _read_cache(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_pickle(path)


def _write_cache(df: pd.DataFrame, path: Path) -> None:
    if path.suffix == ".parquet":
        df.to_parquet(path)
    else:
        df.to_pickle(path)


def _download(ticker: str, interval: str, period: str) -> pd.DataFrame:
    """Thin, mockable wrapper around yfinance. Returns OHLCV with lowercase columns."""
    df = yf.download(ticker, interval=interval, period=period,
                     auto_adjust=False, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    return df[_COLS]


def fetch_bars(ticker: str, interval: str, *, cache_dir: Path, period: str = "2y",
               today: date | None = None, retries: int = 3,
               backoff: float = 0.5) -> pd.DataFrame | None:
    """Fetch OHLCV for one ticker, cached per (interval, ticker, day). Returns None
    on persistent failure (per-ticker isolation: never raises to the caller)."""
    today = today or date.today()
    cache_file = _cache_path(cache_dir, interval, ticker, today)
    if cache_file.exists():
        return _read_cache(cache_file)

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            df = _download(ticker, interval, period)
            if df is None or df.empty:
                raise ValueError("empty frame")
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            _write_cache(df, cache_file)
            return df
        except Exception as err:  # isolation is the whole point: never propagate
            last_err = err
            time.sleep(backoff * (2 ** attempt))
    log.warning("fetch failed for %s %s after %d tries: %s", ticker, interval, retries, last_err)
    return None


def fetch_universe(tickers: list[str], interval: str, *, cache_dir: Path,
                   **kwargs: object) -> dict[str, pd.DataFrame]:
    """Fetch many tickers; silently skip those that fail (isolation)."""
    out: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        df = fetch_bars(ticker, interval, cache_dir=cache_dir, **kwargs)  # type: ignore[arg-type]
        if df is not None:
            out[ticker] = df
    return out

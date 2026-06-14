from collections.abc import Hashable, Mapping

import pandas as pd

_AGG: Mapping[Hashable, str] = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample an OHLCV frame up to a higher timeframe.

    ``rule`` is a pandas offset alias, e.g. "4h" (from 1h bars), "1W" (weekly),
    "1ME" (month-end). Buckets are anchored to the first bar (``origin="start"``)
    so intraday timeframes aggregate contiguous runs rather than being split by a
    midnight anchor. Empty buckets are dropped. Pure: returns a new frame.
    """
    out = df.resample(rule, origin="start").agg(_AGG)
    return out.dropna(subset=["open", "high", "low", "close"])

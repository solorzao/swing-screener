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
    "1ME" (month-end). For tick-like (intraday) rules, buckets are anchored to the
    first bar (``origin="start"``) so they aggregate contiguous runs rather than
    being split by a midnight anchor; ``origin`` has no effect on calendar rules
    (W/ME) and passing it there only emits a warning, so it is omitted for those.
    Empty buckets are dropped. Pure: returns a new frame.
    """
    unit = "".join(c for c in rule if c.isalpha()).lower()
    if unit in {"h", "min", "t", "s", "ms", "us", "ns"}:
        out = df.resample(rule, origin="start").agg(_AGG)
    else:
        out = df.resample(rule).agg(_AGG)
    return out.dropna(subset=["open", "high", "low", "close"])

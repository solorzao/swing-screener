"""Build the JSON-ready lab payload for one (ticker, timeframe): HA + real
candles, EMA 9/21/50/200, MACD(12,26,9), volume, swing-pivot S/R and Fibonacci
retracement. Everything here is a deterministic fact -- plain nullable numbers
on the wire (never Stat dicts), NaN/inf serialised as null.

Indicators are computed over the FULL fetched frame (so EMAs/MACD are warm at
the left edge of the served window) and only the served tail is shipped. EMA
warm-up bars (the first span-1 values) are nulled rather than served biased --
an EMA that hasn't converged reads as absent, not as a fabricated line.
"""

import math
from typing import Any

import pandas as pd

from swing_screener.indicators.heiken_ashi import heiken_ashi
from swing_screener.indicators.trend import ema, macd
from swing_screener.lab.levels import fib_retracement, sr_levels

EMA_SPANS = (9, 21, 50, 200)
MACD_PARAMS = (12, 26, 9)  # fast, slow, signal

# Bars served per timeframe (indicators still compute over the full fetch).
_SERVE_BARS = {"4h": 400, "1d": 260, "1wk": 156, "1mo": 120}

_PIVOT_WIDTH = 3
_CLUSTER_TOL = 0.005
_MAX_LEVELS = 4


def _f(x: object) -> float | None:
    """float or None -- NaN/inf and non-numbers serialise as null."""
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _null_warmup(series: pd.Series, warmup: int) -> pd.Series:
    out = series.copy()
    if warmup > 0:
        out.iloc[: min(warmup, len(out))] = float("nan")
    return out


def _stamp(ts: object, timeframe: str) -> str:
    t = pd.Timestamp(ts)  # type: ignore[arg-type]
    return t.strftime("%Y-%m-%d %H:%M") if timeframe == "4h" else t.strftime("%Y-%m-%d")


def build_lab_payload(ticker: str, timeframe: str, df: pd.DataFrame) -> dict[str, Any]:
    """The lab wire payload. ``df`` is a lowercase-OHLCV frame (data.fetch
    convention), oldest->newest. Raises RuntimeError on an empty frame so the
    router's upstream-503 posture covers it."""
    if df is None or df.empty:
        raise RuntimeError(f"no bars for {ticker} ({timeframe})")

    ha = heiken_ashi(df)
    emas = {
        span: _null_warmup(ema(df["close"], span), span - 1) for span in EMA_SPANS
    }
    fast, slow, signal = MACD_PARAMS
    m = macd(df["close"], fast, slow, signal)
    m["macd"] = _null_warmup(m["macd"], slow - 1)
    m["signal"] = _null_warmup(m["signal"], slow + signal - 2)
    m["hist"] = _null_warmup(m["hist"], slow + signal - 2)

    tail = df.tail(_SERVE_BARS.get(timeframe, 260))
    ha_t = ha.loc[tail.index]
    m_t = m.loc[tail.index]

    candles = [
        {
            "t": _stamp(idx, timeframe),
            "o": _f(row["open"]), "h": _f(row["high"]),
            "l": _f(row["low"]), "c": _f(row["close"]),
            "ha_o": _f(ha_row["ha_open"]), "ha_h": _f(ha_row["ha_high"]),
            "ha_l": _f(ha_row["ha_low"]), "ha_c": _f(ha_row["ha_close"]),
            "v": _f(row["volume"]),
        }
        for (idx, row), (_, ha_row) in zip(tail.iterrows(), ha_t.iterrows())
    ]

    last_close = _f(tail["close"].iloc[-1])
    highs = [float(x) for x in tail["high"].tolist()]
    lows = [float(x) for x in tail["low"].tolist()]

    levels: dict[str, list[dict[str, Any]]] = {"support": [], "resistance": []}
    if last_close is not None:
        sr = sr_levels(highs, lows, last_close, width=_PIVOT_WIDTH,
                       tol_frac=_CLUSTER_TOL, max_per_side=_MAX_LEVELS)
        levels = {
            side: [{"price": _f(lv.price), "touches": lv.touches} for lv in found]
            for side, found in sr.items()
        }

    fib = fib_retracement(highs, lows)
    if fib is not None:
        fib = {
            "high": _f(fib["high"]),
            "low": _f(fib["low"]),
            "direction": fib["direction"],
            "levels": [
                {"ratio": lv["ratio"], "price": _f(lv["price"])}
                for lv in fib["levels"]
            ],
        }

    return {
        "ticker": ticker,
        "timeframe": timeframe,
        "as_of": _stamp(tail.index[-1], timeframe),
        "bar_count": len(candles),
        "last_close": last_close,
        "candles": candles,
        "emas": {str(span): [_f(v) for v in emas[span].loc[tail.index]]
                 for span in EMA_SPANS},
        "macd": {col: [_f(v) for v in m_t[col]] for col in ("macd", "signal", "hist")},
        "levels": levels,
        "fib": fib,
    }

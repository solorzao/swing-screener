"""The lab wire payload: candle alignment (real + HA), EMA warm-up nulls,
MACD series, the served-tail-vs-full-compute split, NaN-as-null, and the
facts-text renderer the Opus prompt consumes."""

import numpy as np
import pandas as pd
import pytest

from swing_screener.lab.payload import EMA_SPANS, build_lab_payload
from swing_screener.lab.report import lab_facts_text


def _frame(n: int = 300, *, start: str = "2025-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(11)
    close = 100 + np.cumsum(rng.normal(0.05, 1.0, n))
    return pd.DataFrame(
        {
            "open": close + rng.normal(0, 0.4, n),
            "high": close + rng.uniform(0.3, 1.8, n),
            "low": close - rng.uniform(0.3, 1.8, n),
            "close": close,
            "volume": rng.integers(1_000_000, 4_000_000, n).astype(float),
        },
        index=pd.bdate_range(start, periods=n),
    )


def test_payload_shape_and_alignment() -> None:
    p = build_lab_payload("TEST", "1d", _frame())
    assert p["ticker"] == "TEST" and p["timeframe"] == "1d"
    assert p["bar_count"] == 260  # served tail, not the full fetch
    assert len(p["candles"]) == 260
    for span in EMA_SPANS:
        assert len(p["emas"][str(span)]) == 260
    for col in ("macd", "signal", "hist"):
        assert len(p["macd"][col]) == 260
    c = p["candles"][-1]
    assert set(c) == {"t", "o", "h", "l", "c", "ha_o", "ha_h", "ha_l", "ha_c", "v"}
    assert p["last_close"] == pytest.approx(c["c"])


def test_ema_warmup_serves_null_never_biased() -> None:
    # 300-bar fetch, 260 served: EMA200's first 199 values are warm-up -> the
    # first ~160 SERVED values are null; the tail is warm and non-null.
    p = build_lab_payload("TEST", "1d", _frame())
    e200 = p["emas"]["200"]
    assert e200[0] is None
    assert e200[-1] is not None
    # EMA9 warmed long before the served window: fully non-null.
    assert all(v is not None for v in p["emas"]["9"])


def test_short_frame_ema_all_null_and_no_crash() -> None:
    p = build_lab_payload("TEST", "1mo", _frame(60))
    assert all(v is None for v in p["emas"]["200"])
    assert p["bar_count"] == 60


def test_indicators_computed_over_full_fetch_not_served_tail() -> None:
    """The served window's left edge must be warm: EMA9 at the first served bar
    equals EMA9 computed over the FULL frame, not a cold restart at the tail
    (a restart would read exactly the first tail close). EMA50's first served
    value is still inside its warm-up here (bar 40 of the fetch < 49) and so
    serves null -- the honest-warm-up rule, asserted alongside."""
    from swing_screener.indicators.trend import ema

    df = _frame()
    p = build_lab_payload("TEST", "1d", df)
    first_served_idx = len(df) - 260  # bar 40 of the 300-bar fetch
    full_ema9 = ema(df["close"], 9)
    assert p["emas"]["9"][0] == pytest.approx(float(full_ema9.iloc[first_served_idx]))
    assert p["emas"]["9"][0] != pytest.approx(float(df["close"].iloc[first_served_idx]))
    assert p["emas"]["50"][0] is None  # inside the 49-bar warm-up: null, not biased


def test_nan_volume_serves_null() -> None:
    df = _frame(80)
    df.loc[df.index[-1], "volume"] = float("nan")
    p = build_lab_payload("^VIX", "1d", df)
    assert p["candles"][-1]["v"] is None


def test_empty_frame_raises_runtime_error() -> None:
    with pytest.raises(RuntimeError):
        build_lab_payload("TEST", "1d", pd.DataFrame())


def test_levels_and_fib_present() -> None:
    p = build_lab_payload("TEST", "1d", _frame())
    assert set(p["levels"]) == {"support", "resistance"}
    for side in ("support", "resistance"):
        for lv in p["levels"][side]:
            assert lv["price"] is not None and lv["touches"] >= 1
    fib = p["fib"]
    assert fib is not None and fib["direction"] in {"up", "down"}
    assert len(fib["levels"]) == 7


def test_4h_stamp_carries_time() -> None:
    n = 90
    rng = np.random.default_rng(3)
    close = 50 + np.cumsum(rng.normal(0, 0.5, n))
    df = pd.DataFrame(
        {"open": close, "high": close + 0.5, "low": close - 0.5,
         "close": close, "volume": [1e6] * n},
        index=pd.date_range("2026-06-01 09:30", periods=n, freq="4h"),
    )
    p = build_lab_payload("TEST", "4h", df)
    assert " " in p["candles"][-1]["t"]  # "%Y-%m-%d %H:%M"


def test_facts_text_names_every_block() -> None:
    p = build_lab_payload("TEST", "1d", _frame())
    text = lab_facts_text({"1d": p})
    assert "== 1d (daily)" in text
    assert "last close:" in text
    assert "heiken ashi:" in text
    assert "MACD(12,26,9):" in text
    assert "volume:" in text
    assert "resistance:" in text and "support:" in text
    assert "fibonacci" in text
    # Missing timeframes are named honestly, never silently absent.
    assert "== 4h (4-hour) ==\nunavailable (fetch failed)" in text


def test_facts_text_insufficient_history_reads_na() -> None:
    p = build_lab_payload("TEST", "1mo", _frame(40))
    text = lab_facts_text({"1mo": p})
    assert "EMA200=n/a (insufficient history)" in text

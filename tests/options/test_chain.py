from datetime import date
from typing import ClassVar

import pandas as pd

from swing_screener.options.chain import (
    ChainSnapshot,
    _spot_from_ticker,
    assess_liquidity,
    snapshot_chain,
)
from swing_screener.options.config import GexConfig


def _fake_raw(ticker: str, max_expiries: int):
    frame = pd.DataFrame([
        {"expiry": date(2026, 7, 17), "strike": 100.0, "right": "C",
         "open_interest": 20_000, "iv": 0.2},
        {"expiry": date(2026, 7, 17), "strike": 95.0, "right": "P",
         "open_interest": 15_000, "iv": 0.25},
    ])
    return 100.0, frame


def test_snapshot_chain_uses_seam() -> None:
    snap = snapshot_chain("SPY", cfg=GexConfig(), fetch=_fake_raw)
    assert isinstance(snap, ChainSnapshot)
    assert snap.spot == 100.0
    assert list(snap.frame.columns) == ["expiry", "strike", "right", "open_interest", "iv"]


def test_spot_nan_fast_info_falls_back_to_daily_close() -> None:
    # yfinance fast_info can surface NaN for lastPrice; NaN is truthy, so it used
    # to sail past the None check and become the GEX map's spot. A non-finite
    # fast_info price must count as missing so the daily-close fallback engages.
    class _NanTicker:
        fast_info: ClassVar[dict[str, float]] = {"lastPrice": float("nan")}

        def history(self, period: str = "1d") -> pd.DataFrame:
            return pd.DataFrame({"Close": [123.45]})

    assert _spot_from_ticker(_NanTicker()) == 123.45  # type: ignore[arg-type]


def test_spot_finite_fast_info_used_directly() -> None:
    class _LiveTicker:
        fast_info: ClassVar[dict[str, float]] = {"lastPrice": 101.5}

        def history(self, period: str = "1d") -> pd.DataFrame:
            raise AssertionError("fallback must not fire when fast_info is finite")

    assert _spot_from_ticker(_LiveTicker()) == 101.5  # type: ignore[arg-type]


def test_liquidity_guard_flags_thin_chain() -> None:
    cfg = GexConfig()
    _, frame = _fake_raw("X", 1)
    report = assess_liquidity(frame, spot=100.0, cfg=cfg)
    assert report.thin is True          # 2 strikes, 35k OI < min_populated_strikes
    assert any("strikes" in r for r in report.reasons)


def test_liquidity_guard_passes_dense_chain() -> None:
    cfg = GexConfig()
    rows = [
        {"expiry": date(2026, 7, 17), "strike": 90.0 + i, "right": "C",
         "open_interest": 2_000, "iv": 0.2}
        for i in range(20)
    ]
    report = assess_liquidity(pd.DataFrame(rows), spot=100.0, cfg=cfg)
    assert report.thin is False and report.reasons == []

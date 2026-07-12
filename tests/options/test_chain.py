from datetime import date

import pandas as pd

from swing_screener.options.chain import ChainSnapshot, assess_liquidity, snapshot_chain
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

"""Deterministic market-facts gathering for the weekly macro screener.

Pure over injected daily frames (no network). Covers the multi-timeframe SPY Heiken-Ashi
alignment (monthly + weekly + daily), VIX rank/spike, yield inversion, and None-safe
degradation when a series is missing.
"""

import numpy as np
import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.market import gather_market_facts

CFG = StrategyConfig()


def _ohlcv(closes, start="2023-01-01"):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    hi = np.maximum(o, c) + 0.5
    lo = np.minimum(o, c) - 0.5
    idx = pd.date_range(start, periods=len(c), freq="D")
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c,
                         "volume": np.full(len(c), 1e6)}, index=idx)


def _rising(n=600, start=300.0, slope=0.3):
    return _ohlcv([start + slope * i for i in range(n)])


def test_monotonic_uptrend_is_aligned_bull():
    facts = gather_market_facts(spy_daily=_rising(), cfg=CFG)
    assert {tf: ha.color for tf, ha in facts.ha.items()} == {"1mo": "bull", "1wk": "bull", "1d": "bull"}
    assert facts.ha_alignment == "aligned_bull"


def test_alignment_classification_and_divergence_note():
    from swing_screener.pipeline.market import TimeframeHA, _alignment
    mixed = {"1mo": TimeframeHA("1mo", "bull", False, 5),
             "1wk": TimeframeHA("1wk", "bear", True, 1),
             "1d": TimeframeHA("1d", "bear", True, 2)}
    label, note = _alignment(mixed)
    assert label == "mixed" and "diverge" in note.lower()
    allbear = {tf: TimeframeHA(tf, "bear", False, 3) for tf in ("1mo", "1wk", "1d")}
    assert _alignment(allbear)[0] == "aligned_bear"


def test_daily_flip_breaks_all_bull_alignment():
    # a long uptrend then a short sharp drop: the daily HA flips bear, breaking the all-bull
    # alignment -- the early trend-shift signal the report must catch.
    closes = [300 + 0.3 * i for i in range(590)] + [477 - 4.0 * i for i in range(12)]
    facts = gather_market_facts(spy_daily=_ohlcv(closes), cfg=CFG)
    assert facts.ha["1d"].color == "bear"
    assert facts.ha_alignment != "aligned_bull"


def test_vix_spike_and_yield_inversion():
    vix = _ohlcv([12.0] * 300 + [40.0])                # last print is a fresh high -> top rank
    tnx = _ohlcv([42.0] * 50)                           # 10y ~4.2
    irx = _ohlcv([45.0] * 50)                           # 3m ~4.5 > 10y -> inverted
    facts = gather_market_facts(spy_daily=_rising(), vix_daily=vix, tnx_daily=tnx,
                                irx_daily=irx, cfg=CFG)
    assert facts.vix is not None and facts.vix_rank is not None and facts.vix_rank > 90
    assert facts.vix_spike is True
    assert facts.yield_inverted is True


def test_none_safe_when_series_missing():
    facts = gather_market_facts(spy_daily=_rising(), cfg=CFG)  # only SPY
    assert facts.vix is None and facts.vix_rank is None and facts.vix_spike is False
    assert facts.ten_year is None and facts.yield_inverted is None and facts.bond_trend is None
    assert facts.ha_alignment == "aligned_bull"  # SPY-only still produces the HA read

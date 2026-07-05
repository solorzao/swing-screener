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
    # v2 fields all degrade to None/False when their series are absent
    assert facts.vix_term_ratio is None and facts.vix_backwardation is False
    assert facts.credit_chg_4w is None and facts.credit_pctile is None
    assert facts.cyc_def_trend is None and facts.breadth_trend is None
    assert facts.recession_prob is None


def test_recession_probability_probit():
    from swing_screener.pipeline.market import _recession_prob
    steep = _recession_prob(4.0, 1.0)      # +3.0pt spread -> low recession odds
    inverted = _recession_prob(3.0, 5.0)   # -2.0pt spread -> high recession odds
    assert steep is not None and inverted is not None
    assert 0.0 <= steep <= 100.0 and 0.0 <= inverted <= 100.0
    assert inverted > steep                # inversion => higher 12m recession probability
    assert _recession_prob(None, 1.0) is None


def test_v2_cross_asset_signals():
    vix = _ohlcv([20.0] * 200 + [35.0])      # spot VIX spikes above the 3-month
    vix3m = _ohlcv([22.0] * 201)
    hyg = _ohlcv([80.0 - 0.05 * i for i in range(120)])   # HY falling vs IG flat -> spreads widen
    lqd = _ohlcv([110.0] * 120)
    xly = _ohlcv([100.0 + 0.2 * i for i in range(120)])   # cyclicals leading defensives
    xlp = _ohlcv([80.0] * 120)
    rsp = _ohlcv([200.0 + 0.6 * i for i in range(120)])   # equal-weight broadening
    facts = gather_market_facts(spy_daily=_rising(), vix_daily=vix, vix3m_daily=vix3m,
                                hyg_daily=hyg, lqd_daily=lqd, xly_daily=xly, xlp_daily=xlp,
                                rsp_daily=rsp, cfg=CFG)
    assert facts.vix_term_ratio is not None and facts.vix_backwardation is True
    assert facts.credit_chg_4w is not None and facts.credit_chg_4w < 0      # spreads widening
    assert facts.credit_pctile is not None
    assert facts.cyc_def_trend == "bull" and facts.cyc_def_chg_4w > 0
    assert facts.breadth_trend in {"bull", "bear", "neutral"}


def test_holiday_nan_tail_falls_back_to_last_real_close():
    """2026-07-05 incident: yfinance's July-4th ^VIX/^TNX/^IRX rows carried NaN closes;
    float(NaN) reached the market_reports INSERT and SQL Server rejected it (TDS 8023).
    A NaN tail must fall back to the last REAL close, never leave as NaN."""
    # real prints ... then the holiday-padded row whose close never printed:
    vix = _ohlcv([18.0, 17.0, 16.15, 16.15])
    vix.loc[vix.index[-1], "close"] = np.nan
    tnx = _ohlcv([4.3, 4.25, 4.2, 4.2])
    tnx.loc[tnx.index[-1], "close"] = np.nan
    irx = _ohlcv([4.5, 4.45, 4.4, 4.4])
    irx.loc[irx.index[-1], "close"] = np.nan

    facts = gather_market_facts(spy_daily=_rising(), vix_daily=vix,
                                tnx_daily=tnx, irx_daily=irx, cfg=CFG)

    assert facts.vix == 16.15                          # Thursday's real print
    assert facts.ten_year == 4.2
    assert facts.three_month == 4.4
    assert facts.yield_inverted is True                # computed from the real closes
    assert facts.vix_rank is not None and facts.vix_rank == facts.vix_rank  # not NaN


def test_all_nan_series_degrades_to_none_not_nan():
    """A series with no valid close at all is an honest None (missing), never NaN."""
    vix = _ohlcv([1.0, 1.0])
    vix["close"] = np.nan

    facts = gather_market_facts(spy_daily=_rising(), vix_daily=vix, cfg=CFG)

    assert facts.vix is None
    assert facts.vix_rank is None
    assert facts.vix_spike is False

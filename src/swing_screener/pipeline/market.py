"""Deterministic market-facts gathering for the weekly macro "Market Weather" screener.

The market-broad sibling of the per-ticker engine: instead of one stock's setup, it snapshots
the WHOLE tape -- SPY Heiken-Ashi across monthly/weekly/daily (and whether they agree), the VIX
percentile regime, the yield-curve inversion, and the bond trend -- as ground-truth facts for the
LLM analyst (which web-searches the rest: Shiller CAPE, Fear & Greed, the 2y curve, econ news).

Pure over injected daily frames (no I/O): the entrypoint fetches via ``data.fetch``; every input
is None-safe so a missing series degrades that field rather than failing the report.
"""

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.data.resample import resample_ohlcv
from swing_screener.pipeline.regime import classify_regime, vix_percentile_rank
from swing_screener.signals.frame import build_frame

# SPY Heiken-Ashi timeframes, anchor (slowest) first: monthly + weekly are the regime, daily is
# the faster, leading signal whose divergence from them is the early trend-shift warning.
_TF_RULES = (("1mo", "1ME"), ("1wk", "1W"), ("1d", None))


@dataclass(frozen=True)
class TimeframeHA:
    timeframe: str       # "1mo" / "1wk" / "1d"
    color: str           # "bull" / "bear" / "neutral"
    flipped: bool        # color differs from the prior bar (a fresh flip)
    bars_in_state: int   # consecutive bars in the current color


@dataclass(frozen=True)
class MarketFacts:
    as_of: date
    spy_close: float | None
    ha: dict[str, TimeframeHA]      # keyed by timeframe (1mo / 1wk / 1d)
    ha_alignment: str               # aligned_bull / aligned_bear / mixed
    ha_alignment_note: str          # human description of agreement/divergence
    spy_vs_200dma: str | None       # "above" / "below" (200DMA regime trend)
    vol_bucket: str | None          # calm / elevated / high (SPY ATR%)
    vix: float | None
    vix_rank: float | None          # trailing-252 percentile
    vix_spike: bool
    ten_year: float | None          # ^TNX last close (Yahoo %-quote)
    three_month: float | None       # ^IRX last close (Yahoo %-quote)
    yield_inverted: bool | None     # 3m > 10y (recession-watch inversion)
    bond_trend: str | None          # TLT weekly HA color
    # --- v2 cross-asset signals (None-safe; each fills a distinct macro dimension) ---
    vix_term_ratio: float | None = None    # ^VIX / ^VIX3M (>1 = backwardation = acute stress)
    vix_backwardation: bool = False
    credit_chg_4w: float | None = None     # HYG/LQD 4wk % change (negative = HY spreads widening)
    credit_pctile: float | None = None     # HYG/LQD trailing-252 percentile (low = stressed)
    cyc_def_trend: str | None = None       # XLY/XLP weekly HA color (bull = risk-on rotation)
    cyc_def_chg_4w: float | None = None
    breadth_trend: str | None = None       # RSP/SPY weekly HA color (bull = broadening participation)
    breadth_chg_4w: float | None = None
    recession_prob: float | None = None    # NY-Fed-style probit on the 10y-3m spread (0-100 %)


def _ratio_series(a: pd.DataFrame | None, b: pd.DataFrame | None) -> pd.Series | None:
    """Close-by-close ratio of two frames on their common dates. None if either is missing."""
    if a is None or b is None or not len(a) or not len(b):
        return None
    idx = a.index.intersection(b.index)
    if not len(idx):
        return None
    s = (a["close"].loc[idx] / b["close"].loc[idx]).dropna()
    return s if len(s) else None


def _last(s: pd.Series | None) -> float | None:
    return float(s.iloc[-1]) if s is not None and len(s) else None


def _last_valid_close(frame: pd.DataFrame | None) -> float | None:
    """Last NON-NaN close, or None when the frame is missing or has no valid close.

    Holiday-padded feeds (yfinance, 2026-07-05: the July-4th ^VIX/^TNX/^IRX rows) print
    NaN closes on the final row; ``iloc[-1]`` passed that NaN downstream and SQL Server
    rejected the INSERT (TDS 8023 -- NaN is not NULL). Falling back to the last real
    print is the honest read; an all-NaN series is an honest None (missing)."""
    if frame is None or not len(frame):
        return None
    valid = frame["close"].dropna()
    return float(valid.iloc[-1]) if len(valid) else None


def _chg_4w(s: pd.Series | None, n: int = 20) -> float | None:
    """% change over the last ~4 weeks (n trading days)."""
    if s is None or len(s) <= n:
        return None
    return float((s.iloc[-1] / s.iloc[-1 - n] - 1.0) * 100.0)


def _synth_ohlcv(s: pd.Series) -> pd.DataFrame:
    """A synthetic OHLCV frame from a close-only ratio series (open = prior close) so the existing
    Heiken-Ashi machinery can read a trend off a series that has no real high/low/volume."""
    o = s.shift(1)
    o.iloc[0] = s.iloc[0]
    pair = pd.concat([o, s], axis=1)
    return pd.DataFrame({"open": o, "high": pair.max(axis=1), "low": pair.min(axis=1),
                         "close": s, "volume": 1.0}, index=s.index)


def _ratio_weekly_trend(s: pd.Series | None, cfg: StrategyConfig) -> str | None:
    """Weekly Heiken-Ashi color of a ratio series (bull / bear / neutral). None if too short."""
    if s is None or len(s) < 15:
        return None
    ha = _tf_ha(resample_ohlcv(_synth_ohlcv(s), "1W"), cfg, "1wk")
    return ha.color if ha else None


def _recession_prob(ten_year: float | None, three_month: float | None) -> float | None:
    """NY-Fed Estrella-Mishkin probit: 12-month recession probability from the 10y-3m spread (in
    percentage points; ^TNX/^IRX are %-quotes). Keeps the recession watch alive after the binary
    inversion flag clears (peak risk is the re-steepening AFTER un-inversion)."""
    if ten_year is None or three_month is None:
        return None
    spread = ten_year - three_month
    z = -0.5333 - 0.6629 * spread
    return float(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))) * 100.0)


def _color(row: pd.Series) -> str:
    if bool(row["bullish"]):
        return "bull"
    if bool(row["bearish"]):
        return "bear"
    return "neutral"


def _tf_ha(df: pd.DataFrame | None, cfg: StrategyConfig, timeframe: str) -> TimeframeHA | None:
    """Heiken-Ashi color / flip / run-length for the last bar of ``df``. None if too short.
    Only the HA classification is used (EMAs may be NaN on short frames -- fine)."""
    if df is None or len(df) < 3:
        return None
    f = build_frame(df, cfg)
    colors = [_color(f.iloc[i]) for i in range(len(f))]
    last = colors[-1]
    flipped = len(colors) >= 2 and colors[-2] != last
    bars = 1
    for c in reversed(colors[:-1]):
        if c == last:
            bars += 1
        else:
            break
    return TimeframeHA(timeframe=timeframe, color=last, flipped=flipped, bars_in_state=bars)


def _alignment(ha: dict[str, TimeframeHA]) -> tuple[str, str]:
    """Classify monthly/weekly/daily agreement and describe it."""
    colors = {tf: h.color for tf, h in ha.items()}
    vals = set(colors.values())
    parts = ", ".join(f"{tf} {colors[tf]}" for tf, _ in _TF_RULES if tf in colors)
    if vals == {"bull"}:
        return "aligned_bull", f"All timeframes agree bullish ({parts})."
    if vals == {"bear"}:
        return "aligned_bear", f"All timeframes agree bearish ({parts})."
    flips = [tf for tf, h in ha.items() if h.flipped]
    note = f"Timeframes DIVERGE ({parts})"
    note += f" -- fresh flip on {', '.join(flips)}." if flips else "."
    return "mixed", note


def gather_market_facts(
    *,
    spy_daily: pd.DataFrame,
    vix_daily: pd.DataFrame | None = None,
    tlt_daily: pd.DataFrame | None = None,
    tnx_daily: pd.DataFrame | None = None,
    irx_daily: pd.DataFrame | None = None,
    vix3m_daily: pd.DataFrame | None = None,
    hyg_daily: pd.DataFrame | None = None,
    lqd_daily: pd.DataFrame | None = None,
    xly_daily: pd.DataFrame | None = None,
    xlp_daily: pd.DataFrame | None = None,
    rsp_daily: pd.DataFrame | None = None,
    cfg: StrategyConfig,
    as_of: date | None = None,
) -> MarketFacts:
    """Snapshot the market into deterministic facts. ``spy_daily`` is required; the rest are
    optional and None-safe (a missing series leaves its fields None)."""
    ha: dict[str, TimeframeHA] = {}
    for tf, rule in _TF_RULES:
        frame = spy_daily if rule is None else resample_ohlcv(spy_daily, rule)
        h = _tf_ha(frame, cfg, tf)
        if h is not None:
            ha[tf] = h
    alignment, note = _alignment(ha)

    reg = classify_regime(spy_daily, cfg)
    spy_vs_200dma = {"bull": "above", "bear": "below"}.get(reg.trend or "")

    vix = _last_valid_close(vix_daily)
    vix_rank = vix_percentile_rank(vix_daily["close"]) if vix_daily is not None else None
    vix_spike = vix_rank is not None and vix_rank >= cfg.vix_spike_rank

    ten_year = _last_valid_close(tnx_daily)
    three_month = _last_valid_close(irx_daily)
    yield_inverted = (three_month > ten_year) if (ten_year is not None and three_month is not None) else None

    bond_trend = None
    if tlt_daily is not None and len(tlt_daily):
        tlt_ha = _tf_ha(resample_ohlcv(tlt_daily, "1W"), cfg, "1wk")
        bond_trend = tlt_ha.color if tlt_ha else None

    # v2 cross-asset signals -- each None-safe (a missing series leaves its fields None).
    vix_term_ratio = _last(_ratio_series(vix_daily, vix3m_daily))
    vix_backwardation = vix_term_ratio is not None and vix_term_ratio > 1.0

    credit_s = _ratio_series(hyg_daily, lqd_daily)
    credit_chg_4w = _chg_4w(credit_s)
    credit_pctile = vix_percentile_rank(credit_s) if credit_s is not None else None

    cyc_s = _ratio_series(xly_daily, xlp_daily)
    breadth_s = _ratio_series(rsp_daily, spy_daily)
    recession_prob = _recession_prob(ten_year, three_month)

    return MarketFacts(
        as_of=as_of or spy_daily.index[-1].date(),
        spy_close=_last_valid_close(spy_daily),
        ha=ha, ha_alignment=alignment, ha_alignment_note=note,
        spy_vs_200dma=spy_vs_200dma, vol_bucket=reg.vol,
        vix=vix, vix_rank=vix_rank, vix_spike=vix_spike,
        ten_year=ten_year, three_month=three_month, yield_inverted=yield_inverted,
        bond_trend=bond_trend,
        vix_term_ratio=vix_term_ratio, vix_backwardation=vix_backwardation,
        credit_chg_4w=credit_chg_4w, credit_pctile=credit_pctile,
        cyc_def_trend=_ratio_weekly_trend(cyc_s, cfg), cyc_def_chg_4w=_chg_4w(cyc_s),
        breadth_trend=_ratio_weekly_trend(breadth_s, cfg), breadth_chg_4w=_chg_4w(breadth_s),
        recession_prob=recession_prob,
    )

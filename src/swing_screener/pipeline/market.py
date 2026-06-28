"""Deterministic market-facts gathering for the weekly macro "Market Weather" screener.

The market-broad sibling of the per-ticker engine: instead of one stock's setup, it snapshots
the WHOLE tape -- SPY Heiken-Ashi across monthly/weekly/daily (and whether they agree), the VIX
percentile regime, the yield-curve inversion, and the bond trend -- as ground-truth facts for the
LLM analyst (which web-searches the rest: Shiller CAPE, Fear & Greed, the 2y curve, econ news).

Pure over injected daily frames (no I/O): the entrypoint fetches via ``data.fetch``; every input
is None-safe so a missing series degrades that field rather than failing the report.
"""

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

    vix = vix_rank = None
    vix_spike = False
    if vix_daily is not None and len(vix_daily):
        vix = float(vix_daily["close"].iloc[-1])
        vix_rank = vix_percentile_rank(vix_daily["close"])
        vix_spike = vix_rank is not None and vix_rank >= cfg.vix_spike_rank

    ten_year = float(tnx_daily["close"].iloc[-1]) if tnx_daily is not None and len(tnx_daily) else None
    three_month = float(irx_daily["close"].iloc[-1]) if irx_daily is not None and len(irx_daily) else None
    yield_inverted = (three_month > ten_year) if (ten_year is not None and three_month is not None) else None

    bond_trend = None
    if tlt_daily is not None and len(tlt_daily):
        tlt_ha = _tf_ha(resample_ohlcv(tlt_daily, "1W"), cfg, "1wk")
        bond_trend = tlt_ha.color if tlt_ha else None

    return MarketFacts(
        as_of=as_of or spy_daily.index[-1].date(),
        spy_close=float(spy_daily["close"].iloc[-1]) if len(spy_daily) else None,
        ha=ha, ha_alignment=alignment, ha_alignment_note=note,
        spy_vs_200dma=spy_vs_200dma, vol_bucket=reg.vol,
        vix=vix, vix_rank=vix_rank, vix_spike=vix_spike,
        ten_year=ten_year, three_month=three_month, yield_inverted=yield_inverted,
        bond_trend=bond_trend,
    )

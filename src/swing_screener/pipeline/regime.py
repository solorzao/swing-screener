"""Market-regime classification for performance attribution.

Stamps each shadow-book fill with the broad market context it was taken in, so
``breakdown(trades, "market_trend")`` / ``breakdown(trades, "market_vol")`` answer WHEN
each engine works -- continuation wants uptrends, reversals want washouts. The proxy is
SPY daily: trend = last close vs its 200-day SMA (bull / bear); volatility = ATR%
bucketed (calm / elevated / high). Pure over a SPY daily frame; the fetch lives in the
pipeline, routed through the same seam as the universe so tests stay offline.
"""

from dataclasses import dataclass

import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.indicators.trend import atr

MARKET_PROXY = "SPY"
_SMA_WINDOW = 200
# SPY daily ATR% bands. Calm markets sit well under 1%; >= 2% is a genuine vol spike.
_VOL_CALM_MAX = 0.01
_VOL_ELEVATED_MAX = 0.02


@dataclass(frozen=True)
class MarketRegime:
    trend: str | None   # "bull" (>= 200DMA) | "bear" (< 200DMA) | None (unknown)
    vol: str | None     # "calm" | "elevated" | "high" | None

    @property
    def known(self) -> bool:
        return self.trend is not None


def classify_regime(spy_daily: pd.DataFrame | None, cfg: StrategyConfig) -> MarketRegime:
    """Classify the market regime from SPY daily bars.

    Needs at least ``_SMA_WINDOW`` bars for the 200-day SMA; fewer (or no data) yields an
    unknown regime so the tag fails safe rather than mislabelling. ``trend`` compares the
    last close to the 200-day SMA; ``vol`` buckets the latest ATR%.
    """
    if spy_daily is None or len(spy_daily) < _SMA_WINDOW:
        return MarketRegime(None, None)

    close = spy_daily["close"]
    last = float(close.iloc[-1])
    sma200 = float(close.tail(_SMA_WINDOW).mean())
    trend = "bull" if last >= sma200 else "bear"

    atr_pct = float(atr(spy_daily, cfg.atr_period).iloc[-1]) / last if last else 0.0
    if atr_pct < _VOL_CALM_MAX:
        vol = "calm"
    elif atr_pct < _VOL_ELEVATED_MAX:
        vol = "elevated"
    else:
        vol = "high"
    return MarketRegime(trend, vol)


# --- VIX percentile-rank overlay (mean-reversion REVERSAL regime gate) ---------------
# Research: the oversold-bounce edge depends on the VIX regime -- in high-VIX panic, "oversold"
# keeps falling. Rank = where today's VIX sits within its own trailing year (point-in-time).
_VIX_RANK_WINDOW = 252          # trailing trading days for the percentile
_VIX_RANK_LOW_MAX = 40.0        # rank < 40 -> "low"
_VIX_RANK_MID_MAX = 70.0        # 40-70 -> "mid"; > 70 -> "high" (panic)


def vix_percentile_rank(vix_close: pd.Series | None) -> float | None:
    """Percentile rank (0-100) of the LAST close within the trailing ``_VIX_RANK_WINDOW``
    closes (inclusive of the current bar, so no lookahead): the fraction of the window
    strictly below the current value. None when there is no data."""
    if vix_close is None or len(vix_close) == 0:
        return None
    window = vix_close.tail(_VIX_RANK_WINDOW)
    current = float(window.iloc[-1])
    return float((window < current).mean() * 100.0)


def vix_bucket(rank: float | None) -> str | None:
    """Bucket a 0-100 VIX rank: low (<40) / mid (40-70) / high (>70). None passes through."""
    if rank is None:
        return None
    if rank < _VIX_RANK_LOW_MAX:
        return "low"
    if rank <= _VIX_RANK_MID_MAX:
        return "mid"
    return "high"

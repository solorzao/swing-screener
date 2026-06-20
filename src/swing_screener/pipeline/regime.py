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

"""Per-timeframe deterministic read for an on-demand single-ticker report.

Pure (no I/O). Reads the LAST bar of each enriched frame (Heiken-Ashi trend, EMA
alignment, RSI, ATR%) and attaches the firing signal for that timeframe, if any.
This module is the deterministic ground truth that the Opus multi-timeframe
analyst (see notify.analysis.analyze_ticker_deep) narrates -- it imports ONLY from
the pipeline, never from notify.analysis, so there is no import cycle.
"""

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.analyze import (
    SignalResult,
    analyze_frames,
    analyze_reversals,
)

# Canonical low->high timeframe order for reads (mirrors pipeline.analyze).
_TIMEFRAME_ORDER: list[str] = ["4h", "1d", "1wk", "1mo"]


@dataclass(frozen=True)
class TimeframeRead:
    timeframe: str
    ha_trend: str          # "bullish" if last HA close >= HA open else "bearish"
    ema_aligned: bool      # fast EMA >= slow EMA on the last bar
    rsi: float
    atr_pct: float         # atr / last close
    setup: SignalResult | None = None
    chart_path: str | None = None


@dataclass(frozen=True)
class TickerReport:
    ticker: str
    name: str
    run_at: datetime
    reads: list[TimeframeRead]
    summary: str = ""
    analysis_text: str = ""
    is_deep: bool = False


def _firing_setup(
    ticker: str, tf: str, frame: pd.DataFrame, cfg: StrategyConfig
) -> SignalResult | None:
    """The signal firing on this timeframe, if any (continuation first, then reversal).

    Prefer a result whose timeframe matches; fall back to the first result overall.
    """
    sigs = analyze_frames(ticker, {tf: frame}, cfg) + analyze_reversals(ticker, {tf: frame}, cfg)
    if not sigs:
        return None
    return next((s for s in sigs if s.timeframe == tf), sigs[0])


def build_ticker_reads(
    ticker: str,
    frames: dict[str, pd.DataFrame],
    cfg: StrategyConfig,
) -> list[TimeframeRead]:
    """Deterministic per-timeframe read for one ticker. Pure (no I/O).

    ``frames`` is the dict produced by ``build_frames`` (keyed by timeframe). Iterate
    the canonical low->high order, skipping timeframes that are absent or empty. For
    each, read the LAST enriched bar and attach the firing signal (if any).
    """
    reads: list[TimeframeRead] = []
    for tf in _TIMEFRAME_ORDER:
        frame = frames.get(tf)
        if frame is None or len(frame) == 0:
            continue
        last = frame.iloc[-1]
        close = float(last["close"])
        reads.append(
            TimeframeRead(
                timeframe=tf,
                ha_trend="bullish" if last["ha_close"] >= last["ha_open"] else "bearish",
                ema_aligned=bool(last["ema_fast"] >= last["ema_slow"]),
                rsi=float(last["rsi"]),
                atr_pct=float(last["atr"]) / close if close else 0.0,
                setup=_firing_setup(ticker, tf, frame, cfg),
            )
        )
    return reads

from dataclasses import dataclass
from typing import cast

import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.signals.build_score import build_score_inputs
from swing_screener.signals.detect import PullbackContext, detect_last_bar
from swing_screener.signals.entry_zone import EntryZone, compute_zone
from swing_screener.signals.frame import build_frame
from swing_screener.signals.score import score_signal

# low -> high timeframe order
TIMEFRAME_ORDER: list[str] = ["4h", "1d", "1wk", "1mo"]

_HORIZON_BY_TF: dict[str, str] = {
    "4h": "short",
    "1d": "medium",
    "1wk": "long",
    "1mo": "long",
}

_AVG_DOLLAR_VOL_WINDOW = 20


@dataclass(frozen=True)
class SignalResult:
    ticker: str
    timeframe: str
    horizon: str
    score: float
    mtf_aligned: bool
    quality_tier: str
    volatility_tier: str
    oversold: bool
    trigger_close: float
    atr: float
    rsi: float
    entry_floor: float
    entry_ceiling: float
    stop: float
    target: float
    # carried for downstream charting (T11) + shadow book (T12); not asserted.
    frame: pd.DataFrame
    ctx: PullbackContext
    zone: EntryZone


def _is_uptrend(frame: pd.DataFrame) -> bool:
    """True iff the frame's last bar is in an uptrend per the detect criteria."""
    last = frame.iloc[-1]
    return bool(last["ema_fast"] > last["ema_slow"] and last["close"] > last["ema_slow"])


def _quality_tier(price: float, avg_dollar_vol: float) -> str:
    if price < 5.0:
        return "penny"
    if avg_dollar_vol < 5e6:
        return "speculative"
    if avg_dollar_vol < 50e6:
        return "mid"
    return "reputable"


def _volatility_tier(atr_pct: float) -> str:
    if atr_pct < 0.02:
        return "low"
    if atr_pct < 0.05:
        return "med"
    return "high"


def _avg_dollar_volume(frame: pd.DataFrame) -> float:
    tail = frame.tail(_AVG_DOLLAR_VOL_WINDOW)
    return float((tail["close"] * tail["volume"]).mean())


def analyze_ticker(
    ticker: str,
    bars_by_tf: dict[str, pd.DataFrame],
    cfg: StrategyConfig,
) -> list[SignalResult]:
    """Run the signal engine across the provided timeframes for one ticker.

    Pure (no I/O). Builds each provided timeframe's enriched frame once, then for
    each timeframe in low->high order emits a SignalResult when a trigger fires
    and a non-degenerate entry zone exists. MTF alignment is read from the
    next-higher provided timeframe's last bar.
    """
    # Build each provided timeframe's enriched frame once; skip empty frames so
    # build_frame is never asked to enrich nothing.
    frames: dict[str, pd.DataFrame] = {}
    for tf in TIMEFRAME_ORDER:
        df = bars_by_tf.get(tf)
        if df is None or len(df) == 0:
            continue
        frames[tf] = build_frame(df, cfg)

    results: list[SignalResult] = []
    for i, tf in enumerate(TIMEFRAME_ORDER):
        frame = frames.get(tf)
        if frame is None:
            continue

        ctx = detect_last_bar(frame, cfg)
        if ctx is None:
            continue

        zone = compute_zone(ctx.trigger_close, ctx.atr, ctx.swing_low, cfg)
        if zone is None:
            continue

        # mtf_aligned: True iff the next-higher provided tf is in an uptrend.
        higher = next((t for t in TIMEFRAME_ORDER[i + 1:] if t in frames), None)
        mtf_aligned = higher is not None and _is_uptrend(frames[higher])

        last_row = cast(dict[str, float | bool], frame.iloc[-1].to_dict())
        score = score_signal(build_score_inputs(ctx, last_row, mtf_aligned))

        price = ctx.trigger_close
        avg_dollar_vol = _avg_dollar_volume(frame)
        atr_pct = ctx.atr / price

        results.append(
            SignalResult(
                ticker=ticker,
                timeframe=tf,
                horizon=_HORIZON_BY_TF[tf],
                score=score,
                mtf_aligned=mtf_aligned,
                quality_tier=_quality_tier(price, avg_dollar_vol),
                volatility_tier=_volatility_tier(atr_pct),
                oversold=ctx.rsi < 35.0,
                trigger_close=ctx.trigger_close,
                atr=ctx.atr,
                rsi=ctx.rsi,
                entry_floor=zone.floor,
                entry_ceiling=zone.ceiling,
                stop=zone.stop,
                target=zone.target,
                frame=frame,
                ctx=ctx,
                zone=zone,
            )
        )

    return results

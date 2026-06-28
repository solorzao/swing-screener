import math
from dataclasses import dataclass
from typing import cast

import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.signals.build_score import build_score_inputs
from swing_screener.signals.detect import PullbackContext, detect_last_bar
from swing_screener.signals.entry_zone import EntryZone, compute_zone
from swing_screener.signals.frame import build_frame
from swing_screener.signals.reversal import (
    ReversalContext,
    ReversalScoreInputs,
    compute_reversal_zone,
    detect_reversal,
    reversal_conviction_tier,
    score_reversal,
)
from swing_screener.signals.score import score_signal

# low -> high timeframe order
TIMEFRAME_ORDER: list[str] = ["4h", "1d", "1wk", "1mo"]

_HORIZON_BY_TF: dict[str, str] = {
    "4h": "short",
    "1d": "medium",
    "1wk": "long",
    "1mo": "long",
}

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
    ctx: PullbackContext | ReversalContext
    zone: EntryZone
    play_type: str = "continuation"   # "continuation" | "reversal"
    strength: str | None = None       # reversal only: "early" | "confirmed"
    # continuation freshness metric ((close-EMA20)/ATR); None for reversal plays.
    extension_atr: float | None = None
    # conviction tier for tiered surfacing + sizing: premium / strong / base (reversal only;
    # continuation defaults to base).
    conviction_tier: str = "base"


def _is_uptrend(frame: pd.DataFrame) -> bool:
    """True iff the frame's last bar is in an uptrend per the detect criteria."""
    last = frame.iloc[-1]
    return bool(last["ema_fast"] > last["ema_slow"] and last["close"] > last["ema_slow"])


def _quality_tier(price: float, avg_dollar_vol: float, cfg: StrategyConfig) -> str:
    if price < cfg.penny_price_max:
        return "penny"
    if avg_dollar_vol < cfg.speculative_dollar_vol_max:
        return "speculative"
    if avg_dollar_vol < cfg.mid_dollar_vol_max:
        return "mid"
    return "reputable"


def _volatility_tier(atr_pct: float, cfg: StrategyConfig) -> str:
    if atr_pct < cfg.low_vol_atr_pct_max:
        return "low"
    if atr_pct < cfg.med_vol_atr_pct_max:
        return "med"
    return "high"


def _avg_dollar_volume(frame: pd.DataFrame, cfg: StrategyConfig) -> float:
    tail = frame.tail(cfg.avg_dollar_vol_window)
    value = float((tail["close"] * tail["volume"]).mean())
    # Fail safe: an all-NaN volume window yields NaN, which would otherwise slip
    # past every "<" check and classify as the most favourable "reputable" tier.
    # Treat non-finite as zero so it lands in "speculative"/"penny" instead.
    return value if math.isfinite(value) else 0.0


def build_frames(
    bars_by_tf: dict[str, pd.DataFrame],
    cfg: StrategyConfig,
) -> dict[str, pd.DataFrame]:
    """Enrich each provided timeframe's OHLCV once. Empty frames are skipped so
    build_frame is never asked to enrich nothing. Pure (no I/O)."""
    frames: dict[str, pd.DataFrame] = {}
    for tf in TIMEFRAME_ORDER:
        df = bars_by_tf.get(tf)
        if df is None or len(df) == 0:
            continue
        frames[tf] = build_frame(df, cfg)
    return frames


def analyze_ticker(
    ticker: str,
    bars_by_tf: dict[str, pd.DataFrame],
    cfg: StrategyConfig,
) -> list[SignalResult]:
    """Run the signal engine across the provided timeframes for one ticker.

    Pure (no I/O). Convenience wrapper that builds the enriched frames and then
    analyzes them (see analyze_frames).
    """
    return analyze_frames(ticker, build_frames(bars_by_tf, cfg), cfg)


def analyze_frames(
    ticker: str,
    frames: dict[str, pd.DataFrame],
    cfg: StrategyConfig,
) -> list[SignalResult]:
    """Analyze pre-built enriched frames for one ticker. For each timeframe in
    low->high order, emit a SignalResult when a trigger fires and a non-degenerate
    entry zone exists. MTF alignment is read from the next-higher provided
    timeframe's last bar. Pure (no I/O)."""
    results: list[SignalResult] = []
    for i, tf in enumerate(TIMEFRAME_ORDER):
        frame = frames.get(tf)
        if frame is None:
            continue

        ctx = detect_last_bar(frame, cfg)
        if ctx is None:
            continue

        # Freshness / anti-chase gate: skip a trigger that already ran too far above
        # the fast EMA (the entry zone would be a chase). This makes the freshness rule
        # part of the strategy end-to-end -- it gates both what the screener surfaces
        # AND what the shadow book forward-tests, so the A/B reflects entries we'd take.
        if cfg.max_extension_atr > 0 and ctx.extension_atr > cfg.max_extension_atr:
            continue

        # Standard (real) highs for overhead-resistance lookup; HA smears real highs.
        recent_highs = frame["high"].tail(
            cfg.target_lookback + 2 * cfg.target_pivot_width
        ).tolist()
        zone = compute_zone(
            ctx.trigger_close, ctx.atr, ctx.swing_low, cfg, recent_highs=recent_highs
        )
        if zone is None:
            continue

        # mtf_aligned: True iff the next-higher provided tf is in an uptrend.
        higher = next((t for t in TIMEFRAME_ORDER[i + 1:] if t in frames), None)
        mtf_aligned = higher is not None and _is_uptrend(frames[higher])

        last_row = cast(dict[str, float | bool], frame.iloc[-1].to_dict())
        score = score_signal(
            build_score_inputs(ctx, last_row, mtf_aligned, cfg.max_extension_atr)
        )

        price = ctx.trigger_close
        avg_dollar_vol = _avg_dollar_volume(frame, cfg)
        atr_pct = ctx.atr / price

        results.append(
            SignalResult(
                ticker=ticker,
                timeframe=tf,
                horizon=_HORIZON_BY_TF[tf],
                score=score,
                mtf_aligned=mtf_aligned,
                quality_tier=_quality_tier(price, avg_dollar_vol, cfg),
                volatility_tier=_volatility_tier(atr_pct, cfg),
                oversold=ctx.rsi < cfg.oversold_rsi_max,
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
                extension_atr=ctx.extension_atr,
            )
        )

    return results


def analyze_reversals(
    ticker: str,
    frames: dict[str, pd.DataFrame],
    cfg: StrategyConfig,
) -> list[SignalResult]:
    """Reversal-play candidates (oversold bounce / relief rally) across the provided
    timeframes. The counter-trend complement to ``analyze_frames``: per timeframe,
    emit a reversal ``SignalResult`` (``play_type="reversal"``) when a reversal
    triggers and yields a non-degenerate zone. Pure (no I/O)."""
    results: list[SignalResult] = []
    for tf in TIMEFRAME_ORDER:
        frame = frames.get(tf)
        if frame is None:
            continue
        ctx = detect_reversal(frame, cfg)
        if ctx is None:
            continue
        zone = compute_reversal_zone(ctx, cfg)
        if zone is None:
            continue

        price = ctx.trigger_close
        avg_dollar_vol = _avg_dollar_volume(frame, cfg)
        atr_pct = ctx.atr / price if price else 0.0
        score = score_reversal(ReversalScoreInputs(
            body_frac=ctx.body_frac, shaved_bottom=ctx.shaved_bottom,
            red_run=ctx.red_run, decline_bars=ctx.decline_bars,
            volume_ratio=ctx.volume_ratio, confirmed=(ctx.strength == "confirmed"),
            min_rsi=ctx.min_rsi, rsi_floor=cfg.reversal_oversold_rsi_max,
        ))
        results.append(SignalResult(
            ticker=ticker, timeframe=tf, horizon=_HORIZON_BY_TF[tf], score=score,
            mtf_aligned=False,  # reversals are counter-trend; no MTF requirement
            quality_tier=_quality_tier(price, avg_dollar_vol, cfg),
            volatility_tier=_volatility_tier(atr_pct, cfg), oversold=True,
            trigger_close=ctx.trigger_close, atr=ctx.atr, rsi=ctx.rsi,
            entry_floor=zone.floor, entry_ceiling=zone.ceiling, stop=zone.stop,
            target=zone.target, frame=frame, ctx=ctx, zone=zone,
            play_type="reversal", strength=ctx.strength,
            conviction_tier=reversal_conviction_tier(
                ctx.volume_ratio, ctx.is_spring, ctx.strength, cfg),
        ))
    return results

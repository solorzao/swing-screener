"""Reversal-play detector: oversold names showing a strong sign of life.

The complement to the pullback-continuation engine. Where that finds shallow
pullbacks inside an uptrend, this finds beaten-down names that recently
capitulated (RSI dipped below ``reversal_oversold_rsi_max``) and just printed a
strong bullish Heiken-Ashi "sign of life" -- the dead-cat-bounce / relief-rally
setup. Two strengths are surfaced and tagged:

* ``early``     -- the latest bar IS the fresh bounce (anticipatory).
* ``confirmed`` -- the prior bar bounced and the latest bar followed through
  (closed above the bounce bar's high).

Targets are mean-reversion levels: the nearest resistance above the entry (the
slow-EMA reclaim or the recent swing high), with a measured-move fallback. All
pure: reads the enriched ``build_frame`` output, never mutates it.
"""

from dataclasses import dataclass

import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.signals.entry_zone import EntryZone

EARLY = "early"
CONFIRMED = "confirmed"


@dataclass(frozen=True)
class ReversalContext:
    trigger_ts: pd.Timestamp
    trigger_close: float
    atr: float
    reversal_low: float    # capitulation low over the decline window (stop reference)
    bounce_high: float     # high of the sign-of-life bar (entry reference)
    rsi: float             # current RSI
    min_rsi: float         # lowest RSI in the oversold window (capitulation depth)
    strength: str          # EARLY | CONFIRMED
    body_frac: float       # bounce-bar body / range (momentum)
    shaved_bottom: bool    # bounce bar closed on its low-side wick (buyers in control)
    volume_ratio: float    # bounce-bar volume / recent average
    ema_slow: float        # slow EMA (reclaim target)
    swing_high: float      # recent swing high (resistance target)


def detect_reversal(f: pd.DataFrame, cfg: StrategyConfig) -> ReversalContext | None:
    """Return a ReversalContext if the last closed bar completes a reversal setup.

    Gates: (1) RSI dipped below ``reversal_oversold_rsi_max`` within the lookback
    (recent capitulation); (2) the decline low sits below the slow EMA (genuinely
    beaten down, not a shallow dip in an uptrend); (3) a Heiken-Ashi "sign of
    life" -- a green bar flipping out of red -- either on the latest bar
    (``early``) or on the prior bar with a follow-through close today
    (``confirmed``). Momentum/volume/depth are left to ``score_reversal``.
    """
    need = max(cfg.ema_slow, cfg.reversal_oversold_lookback, cfg.reversal_decline_bars) + 3
    if len(f) < need:
        return None

    last = f.iloc[-1]
    prev = f.iloc[-2]
    prev2 = f.iloc[-3]

    # 1) recent capitulation: the oversold window dipped below the RSI floor.
    window = f.iloc[-(cfg.reversal_oversold_lookback + 1):]
    min_rsi = float(window["rsi"].min())
    if min_rsi >= cfg.reversal_oversold_rsi_max:
        return None

    # 2) beaten-down context: the decline low is below the slow EMA.
    decline = f.iloc[-(cfg.reversal_decline_bars + 1):]
    reversal_low = float(decline["low"].min())
    if reversal_low >= float(last["ema_slow"]):
        return None

    # 3) the HA flip -- green out of red (early), or a confirmed follow-through.
    if bool(last["bullish"]) and bool(prev["bearish"]):
        strength, bounce = EARLY, last
    elif (bool(prev["bullish"]) and bool(prev2["bearish"]) and bool(last["bullish"])
          and float(last["close"]) > float(prev["high"])):
        strength, bounce = CONFIRMED, prev
    else:
        return None

    avg_vol = float(f["volume"].tail(cfg.avg_dollar_vol_window).mean())
    vol_ratio = float(bounce["volume"]) / avg_vol if avg_vol > 0 else 1.0

    return ReversalContext(
        trigger_ts=f.index[-1],
        trigger_close=float(last["close"]),
        atr=float(last["atr"]),
        reversal_low=reversal_low,
        bounce_high=float(bounce["high"]),
        rsi=float(last["rsi"]),
        min_rsi=min_rsi,
        strength=strength,
        body_frac=float(bounce["body_frac"]),
        shaved_bottom=bool(bounce["shaved_bottom"]),
        volume_ratio=vol_ratio,
        ema_slow=float(last["ema_slow"]),
        swing_high=float(decline["high"].max()),
    )


def compute_reversal_zone(ctx: ReversalContext, cfg: StrategyConfig) -> EntryZone | None:
    """Entry zone for a reversal: enter on/just above the turn bar, stop below the
    capitulation low, target the nearest overhead resistance (EMA reclaim or swing
    high), falling back to a measured ``reversal_target_r_multiple`` move."""
    entry_high = max(ctx.bounce_high, ctx.trigger_close)
    floor = ctx.trigger_close
    ceiling = entry_high + cfg.floor_buffer_atr * ctx.atr
    stop = ctx.reversal_low - cfg.stop_buffer_atr * ctx.atr
    reference = (floor + ceiling) / 2.0
    risk = reference - stop
    if floor >= ceiling or risk <= 0:
        return None

    # nearest resistance strictly above the ceiling -> the realistic bounce target.
    resistances = sorted(r for r in (ctx.ema_slow, ctx.swing_high) if r > ceiling)
    target = resistances[0] if resistances else reference + cfg.reversal_target_r_multiple * risk
    return EntryZone(floor=floor, ceiling=ceiling, stop=stop, target=target,
                     risk=risk, reference=reference)


@dataclass(frozen=True)
class ReversalScoreInputs:
    min_rsi: float          # capitulation depth (lower = deeper)
    rsi_floor: float        # the oversold threshold (cfg.reversal_oversold_rsi_max)
    body_frac: float        # bounce body strength
    shaved_bottom: bool
    volume_ratio: float     # bounce volume / average
    confirmed: bool


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_reversal(s: ReversalScoreInputs) -> float:
    """0..1 conviction for a reversal play: deeper oversold + a stronger, higher-
    volume bounce + confirmation all raise the score."""
    depth = _clip01((s.rsi_floor - s.min_rsi) / s.rsi_floor) if s.rsi_floor > 0 else 0.0
    momentum = 0.6 * _clip01(s.body_frac) + 0.4 * (1.0 if s.shaved_bottom else 0.0)
    volume = _clip01(s.volume_ratio - 1.0)        # 2x average volume saturates
    confirmation = 1.0 if s.confirmed else 0.0
    score = 0.35 * depth + 0.30 * momentum + 0.20 * volume + 0.15 * confirmation
    return _clip01(score)

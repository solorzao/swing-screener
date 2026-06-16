"""Reversal-play detector: a Heiken-Ashi reversal out of a downtrend.

The HA-first complement to the pullback-continuation engine. Like the rest of the
system this is **Heiken-Ashi centric**: the GATE is HA structure -- a run of red
HA candles (downtrend) that flips green -- on a name beaten below its slow EMA.
RSI is NOT a filter here; it only feeds the score (a deeper-oversold bounce ranks
higher). This catches dead-cat bounces / relief rallies. Two strengths are tagged:

* ``early``     -- the latest bar IS the fresh green flip (anticipatory).
* ``confirmed`` -- the prior bar flipped and the latest bar followed through
  (closed above the flip bar's high).

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
    reversal_low: float    # capitulation low over the decline window (retracement BASE)
    recent_low: float      # low of the last few bars (the bounce's base -> the STOP)
    bounce_high: float     # high of the sign-of-life bar (entry reference)
    rsi: float             # current RSI
    min_rsi: float         # lowest RSI in the lookback window (oversold depth, scoring only)
    strength: str          # EARLY | CONFIRMED
    body_frac: float       # flip-bar body / range (HA momentum)
    shaved_bottom: bool    # flip bar closed on its low-side wick (buyers in control)
    red_run: int           # bearish HA bars in the decline window (downtrend strength)
    decline_bars: int      # decline window size (to normalize red_run)
    volume_ratio: float    # flip-bar volume / recent average
    ema_slow: float        # slow EMA (reclaim resistance)
    decline_high: float    # prior swing high over a longer lookback (retracement target)


def detect_reversal(f: pd.DataFrame, cfg: StrategyConfig) -> ReversalContext | None:
    """Return a ReversalContext if the last closed bar completes a reversal setup.

    HA-CENTRIC gates: (1) a Heiken-Ashi downtrend -- at least
    ``reversal_min_bearish_bars`` red HA candles in the decline window;
    (2) the decline low sits below the slow EMA (genuinely beaten down, not a
    shallow dip in an uptrend); (3) the HA flip -- a green bar out of red, either
    on the latest bar (``early``) or on the prior bar with a follow-through close
    today (``confirmed``). RSI is NOT gated here; ``min_rsi`` is carried only so
    ``score_reversal`` can reward a deeper-oversold bounce.
    """
    need = max(cfg.ema_slow, cfg.reversal_oversold_lookback, cfg.reversal_decline_bars) + 3
    if len(f) < need:
        return None

    last = f.iloc[-1]
    prev = f.iloc[-2]
    prev2 = f.iloc[-3]

    # 1) HA downtrend: enough red HA candles in the decline window. The green flip
    #    bar(s) are bullish, so they don't count toward the red run.
    decline = f.iloc[-(cfg.reversal_decline_bars + 1):]
    red_run = int(decline["bearish"].sum())
    if red_run < cfg.reversal_min_bearish_bars:
        return None

    # 2) beaten-down context: the decline low is below the slow EMA.
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

    # RSI depth is scoring-only (no gate): the lowest RSI over the lookback window.
    min_rsi = float(f.iloc[-(cfg.reversal_oversold_lookback + 1):]["rsi"].min())
    avg_vol = float(f["volume"].tail(cfg.avg_dollar_vol_window).mean())
    vol_ratio = float(bounce["volume"]) / avg_vol if avg_vol > 0 else 1.0
    # The STOP rides the recent swing low (the bounce's base) -- a failed reversal
    # rolls back through here -- NOT the months-deep capitulation low. The TARGET
    # references the prior decline's high over a longer lookback (for a retracement).
    recent_low = float(f.iloc[-cfg.reversal_stop_lookback:]["low"].min())
    decline_high = float(f.iloc[-(cfg.reversal_target_lookback + 1):]["high"].max())

    return ReversalContext(
        trigger_ts=f.index[-1],
        trigger_close=float(last["close"]),
        atr=float(last["atr"]),
        reversal_low=reversal_low,
        recent_low=recent_low,
        bounce_high=float(bounce["high"]),
        rsi=float(last["rsi"]),
        min_rsi=min_rsi,
        strength=strength,
        body_frac=float(bounce["body_frac"]),
        shaved_bottom=bool(bounce["shaved_bottom"]),
        red_run=red_run,
        decline_bars=cfg.reversal_decline_bars,
        volume_ratio=vol_ratio,
        ema_slow=float(last["ema_slow"]),
        decline_high=decline_high,
    )


def compute_reversal_zone(ctx: ReversalContext, cfg: StrategyConfig) -> EntryZone | None:
    """Entry zone for a reversal, tuned for a sane reward:risk.

    Enter on/just above the turn bar. STOP below the recent swing low (the bounce's
    base), capped at ``reversal_max_stop_atr`` ATRs below entry -- not the deep
    capitulation low, which made stops absurdly wide on bounces that had already run
    up. TARGET is a retracement of the prior decline (``reversal_retrace_frac`` of
    ``decline_high - reversal_low``) or the EMA reclaim, whichever is further; a
    modest measured move backstops the rare case where both sit below entry.
    """
    entry_high = max(ctx.bounce_high, ctx.trigger_close)
    floor = ctx.trigger_close
    ceiling = entry_high + cfg.floor_buffer_atr * ctx.atr
    # Stop: the higher (tighter) of the recent swing low and an ATR-capped floor.
    stop_ref = max(ctx.recent_low, ctx.trigger_close - cfg.reversal_max_stop_atr * ctx.atr)
    stop = stop_ref - cfg.stop_buffer_atr * ctx.atr
    reference = (floor + ceiling) / 2.0
    risk = reference - stop
    if floor >= ceiling or risk <= 0:
        return None

    # Target: a retracement of the decline, or the EMA reclaim -- the further one.
    retrace = ctx.reversal_low + cfg.reversal_retrace_frac * (ctx.decline_high - ctx.reversal_low)
    target = max(retrace, ctx.ema_slow)
    if target <= ceiling:  # both already below entry -> a modest measured move
        target = ceiling + cfg.reversal_target_r_multiple * risk
    return EntryZone(floor=floor, ceiling=ceiling, stop=stop, target=target,
                     risk=risk, reference=reference)


@dataclass(frozen=True)
class ReversalScoreInputs:
    body_frac: float        # HA flip-bar body strength
    shaved_bottom: bool     # HA flip-bar quality (buyers in control)
    red_run: int            # bearish HA bars in the decline (downtrend strength)
    decline_bars: int       # decline window (to normalize red_run)
    volume_ratio: float     # flip volume / average
    confirmed: bool
    min_rsi: float          # oversold depth (lower = deeper) -- a confirm, not a gate
    rsi_floor: float        # the oversold reference (cfg.reversal_oversold_rsi_max)


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_reversal(s: ReversalScoreInputs) -> float:
    """0..1 conviction. HA-CENTRIC: the flip-bar strength and the downtrend it
    reverses dominate; volume + confirmation matter; oversold RSI depth is only a
    smaller confirmation (so the screener isn't RSI-driven)."""
    bounce = 0.6 * _clip01(s.body_frac) + 0.4 * (1.0 if s.shaved_bottom else 0.0)  # HA flip
    downtrend = _clip01(s.red_run / max(s.decline_bars, 1))                        # HA red run
    volume = _clip01(s.volume_ratio - 1.0)        # 2x average volume saturates
    confirmation = 1.0 if s.confirmed else 0.0
    depth = _clip01((s.rsi_floor - s.min_rsi) / s.rsi_floor) if s.rsi_floor > 0 else 0.0
    # HA factors (bounce+downtrend) = 0.55; RSI depth only 0.15.
    score = 0.35 * bounce + 0.20 * downtrend + 0.15 * volume + 0.15 * confirmation + 0.15 * depth
    return _clip01(score)

import math
from dataclasses import dataclass, replace

import pandas as pd

from swing_screener.config import StrategyConfig


@dataclass(frozen=True)
class PullbackContext:
    trigger_ts: pd.Timestamp
    trigger_close: float
    atr: float
    swing_low: float          # lowest low across the pullback window
    pullback_bars: int
    shaved_bottom: bool       # trigger quality flag
    rsi: float
    # how far the trigger close already sits above the fast EMA, in ATR units.
    # A freshness/anti-chase measure: a large value means the move "already ran".
    # Defaulted so callers/tests that construct a context by hand stay valid.
    extension_atr: float = 0.0


def _quality_gates_pass(f: pd.DataFrame, last, pullback: list, swing_low: float,
                        atr: float, cfg: StrategyConfig) -> bool:
    """Tier-A continuation quality gates (edge tournament round 1). Each is a detection-only
    lever, default no-op; returns False to reject the trigger. Computed from the enriched
    frame only -- no new indicators."""
    # volume thrust: the resumption bar prints on above-average volume (real demand).
    # Denominator A/B (vol_thrust_excl_pullback): the legacy baseline window INCLUDES the
    # pullback's own dried-up volume, flattering thrust ratios on longer pullbacks; the
    # excl variant measures the window BEFORE the pullback (matching the dry-up gate).
    if cfg.vol_thrust_min > 0:
        n = cfg.vol_avg_window
        if cfg.vol_thrust_excl_pullback:
            k = len(pullback)
            base_vol = f["volume"].iloc[-(n + 1 + k):-(k + 1)].mean()
        else:
            base_vol = f["volume"].iloc[-(n + 1):-1].mean()
        rvol = float(last["volume"]) / base_vol if base_vol and base_vol > 0 else 1.0
        # NaN volume rows are deliberately kept at the download seam (index
        # tickers), so rvol can be NaN -- and NaN < min is False, silently
        # no-opping the gate. An unmeasurable thrust rejects (2026-07 audit).
        if math.isnan(rvol) or rvol < cfg.vol_thrust_min:
            return False
    # trend strength: EMA20 must sit a meaningful (ATR-normalized) distance above EMA50
    if cfg.min_ema_sep_atr > 0 and atr:
        if (float(last["ema_fast"]) - float(last["ema_slow"])) / atr < cfg.min_ema_sep_atr:
            return False
    # momentum re-acceleration: MACD histogram positive AND turning up
    if cfg.require_macd_hook and not (float(last["macd_hist"]) > 0 and bool(last["macd_hist_rising"])):
        return False
    # bull-range floor: RSI must still hold the uptrend pullback zone
    if cfg.rsi_min_trigger > 0 and float(last["rsi"]) < cfg.rsi_min_trigger:
        return False
    # per-name volatility floor: low-ATR% names can't travel to target (the worst cohort)
    if cfg.min_atr_pct > 0:
        close = float(last["close"])
        if close and atr / close < cfg.min_atr_pct:
            return False
    # trigger conviction: a strong HA body, plus a shaved bottom or small lower wick
    if cfg.min_trigger_body_frac > 0:
        if float(last["body_frac"]) < cfg.min_trigger_body_frac:
            return False
        rng = float(last["ha_high"]) - float(last["ha_low"])
        lower_wick = min(float(last["ha_open"]), float(last["ha_close"])) - float(last["ha_low"])
        lower_wick_frac = lower_wick / rng if rng > 0 else 0.0
        if not (bool(last["shaved_bottom"]) or lower_wick_frac <= cfg.max_trigger_lower_wick_frac):
            return False
    # depth-to-value: the pullback reached the EMA20 band (within tol) AND held above EMA50
    if cfg.require_value_band and atr:
        ema_fast = float(last["ema_fast"])
        ema_slow = float(last["ema_slow"])
        reached = swing_low <= ema_fast + cfg.band_touch_tol_atr * atr
        held = swing_low >= ema_slow + cfg.band_floor_buf_atr * atr
        if not (reached and held):
            return False
    # orderly shape: no single violent pullback bar and a controlled total drop
    if cfg.require_orderly_pullback and atr:
        max_bar_atr = max(float(b["high"]) - float(b["low"]) for b in pullback) / atr
        pull_high = max(float(b["high"]) for b in pullback)
        drop_atr = (pull_high - swing_low) / atr
        if max_bar_atr > cfg.max_pullback_bar_atr or drop_atr > cfg.max_pullback_drop_atr:
            return False
    # volume dry-up: the pullback must trade on contracting volume vs the pre-pullback baseline
    if cfg.pullback_vol_dryup_max > 0:
        n, k = cfg.vol_avg_window, len(pullback)
        base = f["volume"].iloc[-(n + 1 + k):-(k + 1)].mean()
        pull_vol = sum(float(b["volume"]) for b in pullback) / k
        dryup = pull_vol / base if base and base > 0 else 1.0
        # same NaN fail-safe as the thrust gate: an unmeasurable dry-up rejects
        if math.isnan(dryup) or dryup > cfg.pullback_vol_dryup_max:
            return False
    # pocket pivot: the up trigger bar's volume must exceed the worst recent down-day volume
    if cfg.require_pocket_pivot:
        window = f.iloc[-(cfg.pocket_pivot_lookback + 1):-1]
        down = window.loc[window["close"] < window["open"], "volume"]
        down_vol_max = float(down.max()) if len(down) else 0.0
        if not (float(last["close"]) > float(last["open"]) and float(last["volume"]) > down_vol_max):
            return False
    return True


def _detect_confirmed_breakout(f: pd.DataFrame, cfg: StrategyConfig) -> PullbackContext | None:
    """The ``cont_confirm_window`` trigger: fire on the FIRST close above the flip bar's
    high within the window, with the setup's STRUCTURE validated as-of the flip.

    The continuation analog of ``reversal_confirm_window`` (whose late-confirm cohort
    graded +0.110R): the trailing HA-green run ending today locates the flip (its oldest
    bar); the flip must be a fully valid LEGACY trigger on the frame ending there
    (uptrend, pullback, quality gates -- evaluated by recursing with the window off);
    today's close is the first of the run above the flip's high (single-fire). Trigger
    price and the extension/freshness metric are TODAY's -- the anti-chase gate judges
    the bar actually entered on. Never fires on the flip bar itself (g >= 2), so the
    default and windowed variants are disjoint entry-timing cohorts.
    """
    g = 0
    while g < len(f) - 1 and bool(f.iloc[-1 - g]["bullish"]):
        g += 1
    if g < 2 or (g - 1) > cfg.cont_confirm_window:
        return None
    last = f.iloc[-1]
    flip = f.iloc[-g]
    if float(last["close"]) <= float(flip["high"]):
        return None
    between = f["close"].iloc[len(f) - g + 1: len(f) - 1]
    if len(between) and float(between.max()) > float(flip["high"]):
        return None  # an earlier run bar already confirmed; today is not the first
    base_ctx = detect_last_bar(f.iloc[: len(f) - g + 1],
                               replace(cfg, cont_confirm_window=0))
    if base_ctx is None:
        return None
    atr = float(last["atr"])
    # the recursion above validated the FLIP bar; atr/rsi here are TODAY's and can
    # be NaN independently -- NaN is truthy, so `if atr` would ship extension=NaN
    # and a NaN-atr context (NaN stops/targets). Fail safe: no signal.
    if not (math.isfinite(atr) and math.isfinite(float(last["rsi"]))):
        return None
    extension_atr = (float(last["close"]) - float(last["ema_fast"])) / atr if atr else 0.0
    return PullbackContext(
        trigger_ts=f.index[-1],
        trigger_close=float(last["close"]),
        atr=atr,
        swing_low=base_ctx.swing_low,
        pullback_bars=base_ctx.pullback_bars,
        shaved_bottom=bool(last["shaved_bottom"]),
        rsi=float(last["rsi"]),
        extension_atr=extension_atr,
    )


def detect_last_bar(f: pd.DataFrame, cfg: StrategyConfig) -> PullbackContext | None:
    """Return a PullbackContext if the last closed bar of ``f`` is a valid
    pullback-continuation long trigger, else None.

    ``f`` is the enriched frame from ``build_frame`` (HA + EMAs + ATR + RSI +
    classification). Pure: reads only, never mutates ``f``. With
    ``cont_confirm_window > 0`` the trigger is the breakout-confirmation variant
    (see ``_detect_confirmed_breakout``); 0 is the incumbent flip-bar trigger.
    """
    if cfg.cont_confirm_window > 0:
        return _detect_confirmed_breakout(f, cfg)
    if len(f) < cfg.ema_slow + cfg.max_pullback_bars + 2:
        return None

    last = f.iloc[-1]
    prev = f.iloc[-2]
    # 1) uptrend context at the trigger bar
    if not (last["ema_fast"] > last["ema_slow"] and last["close"] > last["ema_slow"]):
        return None
    # trigger: the incumbent bullish HA flip, or (experiment) a raw bullish outside bar
    # whose range engulfs the prior bar on BOTH sides and closes up.
    if cfg.trigger_kind == "outside_bar":
        is_outside = last["high"] > prev["high"] and last["low"] < prev["low"]
        if not (is_outside and last["close"] > last["open"]):
            return None
    elif not bool(last["bullish"]):
        return None

    # 2) walk back over the immediately preceding bars looking for the pullback
    #    (contiguous bearish/zone bars), and require a bearish shaved head within it.
    pullback = []
    saw_shaved_head = False
    for k in range(2, cfg.max_pullback_bars + 2):
        bar = f.iloc[-k]
        if bool(bar["bearish"]) or bool(bar["zone"]):
            pullback.append(bar)
            if bool(bar["shaved_head"]):
                saw_shaved_head = True
        else:
            break

    if len(pullback) < cfg.min_pullback_bars or not saw_shaved_head:
        return None

    # use the real traded low (not the smoothed HA low) so stops/zones in Task 7
    # sit off actual price, and the shallow-pullback gate reflects true price.
    swing_low = min(b["low"] for b in pullback)
    # 3) shallow pullback: stayed above ema_slow (continuation, not reversal)
    if swing_low <= last["ema_slow"]:
        return None
    # 3b) entry-depth gate (experiment): require the pullback to have actually reached into
    # the EMA20-EMA50 band, i.e. price pulled back to value rather than barely dipping while
    # still extended above the fast EMA.
    if cfg.require_band_touch and swing_low > last["ema_fast"]:
        return None

    atr = float(last["atr"])
    # NaN fails safe (2026-07 audit): NaN is truthy and every NaN comparison is
    # False, so a NaN ATR/RSI would silently no-op the truthiness-guarded gates
    # below and then ship NaN stops/targets/scores in the context. No finite
    # indicator at the trigger bar -> no signal.
    if not (math.isfinite(atr) and math.isfinite(float(last["rsi"]))):
        return None
    # 3c) Tier-A quality gates (edge tournament round 1) -- all default no-op.
    if not _quality_gates_pass(f, last, pullback, swing_low, atr, cfg):
        return None

    # Extension above the fast EMA in ATR units -- the anti-chase/freshness measure.
    # The gate itself lives in analyze_frames (policy); detect only reports the metric.
    extension_atr = (float(last["close"]) - float(last["ema_fast"])) / atr if atr else 0.0

    return PullbackContext(
        trigger_ts=f.index[-1],
        trigger_close=float(last["close"]),
        atr=atr,
        swing_low=float(swing_low),
        pullback_bars=len(pullback),
        shaved_bottom=bool(last["shaved_bottom"]),
        rsi=float(last["rsi"]),
        extension_atr=extension_atr,
    )

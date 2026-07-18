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

import math
from dataclasses import dataclass

import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.signals.entry_zone import EntryZone

EARLY = "early"
CONFIRMED = "confirmed"

# score_reversal's fallback weights when no cfg is passed (identical to the field
# defaults); module-level so the per-signal hot path never rebuilds a StrategyConfig.
_DEFAULT_CFG = StrategyConfig()


@dataclass(frozen=True)
class ReversalContext:
    trigger_ts: pd.Timestamp
    trigger_close: float
    atr: float
    reversal_low: float    # the low the bounce came from (decline window) -> stop + retracement base
    bounce_high: float     # high of the sign-of-life bar (top of the bounce)
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
    is_spring: bool = False  # bounce undercut a prior support then reclaimed (Wyckoff spring)
    # bars from the flip to the confirmation close (1 = next-bar, up to the confirm
    # window); 0 for EARLY. The only ordering signal found to separate outcomes on the
    # confirmed book (2026-07-03 rank sweep) -- feeds score_reversal's lag term.
    confirm_lag: int = 0


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
    # NaN fails safe (2026-07 audit; mirrors detect.py): the HA gates below never
    # validate atr/rsi, NaN is truthy, and every NaN comparison is False -- so a
    # NaN indicator sailed through and shipped a NaN-atr context, from which
    # compute_reversal_zone built stop = low - buffer*NaN = NaN (its risk <= 0
    # rejection is itself NaN-defeated). No finite indicator -> no signal.
    if not (math.isfinite(float(last["atr"])) and math.isfinite(float(last["rsi"]))):
        return None

    # 1) the HA flip. ``g`` is the trailing HA-green run ending today; the FLIP bar is its
    #    oldest bar (green out of red). g==1 -> the flip IS today (early). g>=2 -> a
    #    confirmed follow-through when today prints the FIRST close above the flip bar's
    #    high, within ``reversal_confirm_window`` bars of the flip. At the default window
    #    of 1 only g==2 can confirm, which is exactly the legacy 3-bar pattern; a wider
    #    window lets a green pause bar (an inside day digesting a violent flip) confirm a
    #    bar or two late instead of killing the setup forever (2026-07 rotation audit).
    g = 0
    while g < len(f) - 1 and bool(f.iloc[-1 - g]["bullish"]):
        g += 1
    if g == 0 or not bool(f.iloc[-g - 1]["bearish"]):
        return None
    if g == 1:
        strength, bounce = EARLY, last
    else:
        if g - 1 > cfg.reversal_confirm_window:
            return None
        flip = f.iloc[-g]
        if float(last["close"]) <= float(flip["high"]):
            return None
        between = f["close"].iloc[len(f) - g + 1: len(f) - 1]
        if len(between) and float(between.max()) > float(flip["high"]):
            return None  # an earlier bar of the run already confirmed; today is not the first
        strength, bounce = CONFIRMED, flip

    # 2) HA downtrend: enough red HA candles in the decline window. The green flip
    #    bar(s) are bullish, so they don't count toward the red run. A late confirm
    #    (g > 2) measures the window as-of the FLIP bar -- otherwise the recovery greens
    #    displace the reds the gate is looking for.
    gate_f = f if g <= 2 else f.iloc[: len(f) - g + 1]
    decline = gate_f.iloc[-(cfg.reversal_decline_bars + 1):]
    red_run = int(decline["bearish"].sum())
    if red_run < cfg.reversal_min_bearish_bars:
        return None

    # 3) beaten-down context: the decline low is below the slow EMA. Finiteness is
    #    explicit: reversal_low >= NaN is False, so a NaN EMA (or an all-NaN
    #    decline window) would slip past this rejection and ship NaN into the
    #    context -- and reversal_low is the STOP base. Unknown -> reject.
    reversal_low = float(decline["low"].min())
    ema_slow = float(last["ema_slow"])
    if not (math.isfinite(reversal_low) and math.isfinite(ema_slow)):
        return None
    if reversal_low >= ema_slow:
        return None

    # Wyckoff spring: the bounce bar undercuts a recent support low (over the lookback ending
    # spring_gap bars back) then closes back above it -- a shakeout. Computed ALWAYS (carried on
    # the context for surfacing/sizing tiers); require_spring only decides whether to GATE on it.
    _win = f["low"].iloc[-(cfg.spring_lookback + cfg.spring_gap):-cfg.spring_gap]
    _prior_support = float(_win.min()) if len(_win) else float("inf")
    is_spring = float(bounce["low"]) < _prior_support and float(bounce["close"]) > _prior_support
    if cfg.require_spring and not is_spring:
        return None

    # RSI depth is scoring-only (no gate): the lowest RSI over the lookback window.
    min_rsi = float(f.iloc[-(cfg.reversal_oversold_lookback + 1):]["rsi"].min())
    avg_vol = float(f["volume"].tail(cfg.avg_dollar_vol_window).mean())
    vol_ratio = float(bounce["volume"]) / avg_vol if avg_vol > 0 else 1.0
    # NaN flip volume (rows kept at the download seam -- index tickers) reads as
    # the same neutral 1.0 as a missing baseline: a NaN ratio would defeat both
    # rvol gates below (NaN comparisons are False), full-credit the score's
    # volume term, and ship NaN into the context.
    if not math.isfinite(vol_ratio):
        vol_ratio = 1.0
    # experiment (edge-discovery exp 5): A/B the flip-bar volume sign. Low-vol-confirmation
    # gate (supply exhausted) vs high-vol-confirmation gate (demand stepped in); 0 = off.
    if cfg.reversal_max_flip_rvol > 0 and vol_ratio > cfg.reversal_max_flip_rvol:
        return None
    if cfg.reversal_min_flip_rvol > 0 and vol_ratio < cfg.reversal_min_flip_rvol:
        return None
    # RS-leadership gate (edge-discovery wave 2): only buy oversold names HOLDING UP vs SPY --
    # the RS line (close/spy_close) must be above its own MA at the bounce. No-op when the rs
    # column is absent (SPY not provided) or NaN (SPY gap) -- fail open, no market data penalty.
    if cfg.require_rs_leader and "rs" in f.columns:
        rs_now = float(f["rs"].iloc[-1])
        if rs_now == rs_now and rs_now <= float(f["rs"].tail(cfg.rs_ma_window).mean()):
            return None
    # The prior decline's high over a longer lookback -- the breakdown level the relief
    # rally targets (and the base for the target retracement).
    decline_high = float(f.iloc[-(cfg.reversal_target_lookback + 1):]["high"].max())

    # Entry-band anchor: legacy = the flip bar's high, which for CONFIRMED sits BELOW the
    # confirmation close by definition (the signal is born above its own ceiling). The
    # anchor_confirmation knob re-anchors on the top of the bounce-so-far (max high of the
    # trailing green run) so the band tracks where the bounce actually is.
    bounce_high = float(bounce["high"])
    if strength == CONFIRMED and cfg.reversal_anchor_confirmation:
        bounce_high = float(f["high"].iloc[len(f) - g:].max())

    return ReversalContext(
        trigger_ts=f.index[-1],
        trigger_close=float(last["close"]),
        atr=float(last["atr"]),
        reversal_low=reversal_low,
        bounce_high=bounce_high,
        rsi=float(last["rsi"]),
        min_rsi=min_rsi,
        strength=strength,
        body_frac=float(bounce["body_frac"]),
        shaved_bottom=bool(bounce["shaved_bottom"]),
        red_run=red_run,
        decline_bars=cfg.reversal_decline_bars,
        volume_ratio=vol_ratio,
        ema_slow=ema_slow,
        decline_high=decline_high,
        is_spring=is_spring,
        confirm_lag=g - 1 if strength == CONFIRMED else 0,
    )


def compute_reversal_zone(ctx: ReversalContext, cfg: StrategyConfig) -> EntryZone | None:
    """Entry zone for a reversal -- a PULLBACK buy, not a chase.

    A confirmed bounce has already run up, so chasing it (entering at the top) leaves
    no room: the stop is far at support and the target is just overhead. Instead the
    entry is a limit into a PULLBACK of the bounce (the ``reversal_pullback_*`` band of
    ``reversal_low -> bounce_high``); the STOP sits below the bounce's origin
    (``reversal_low`` -- a break back through there fails the reversal); the TARGET is a
    deep retracement of the prior decline toward the breakdown level. That geometry
    gives a sane reward:risk. A modest measured move backstops a degenerate target.
    """
    bounce_range = ctx.bounce_high - ctx.reversal_low
    if bounce_range <= 0:
        return None
    ceiling = ctx.bounce_high - cfg.reversal_pullback_shallow * bounce_range
    # "Buy at yesterday's close or better": the ceiling is the trigger close itself, so a
    # V-bounce that never retraces into the band can still fill (any next-bar trade at or
    # below the trigger close does it). The floor keeps the deep-retrace bound.
    if cfg.reversal_ceiling_at_close:
        ceiling = ctx.trigger_close
    floor = ctx.bounce_high - cfg.reversal_pullback_deep * bounce_range
    stop = ctx.reversal_low - cfg.stop_buffer_atr * ctx.atr
    # R is measured from the price the order actually fills at -- the entry CEILING (the
    # buy-at-or-below limit; for a reversal pullback this is the shallow-pullback / highest
    # price in the band). Sizing (insight.size_order), the shadow-book fill
    # (fill.resolve_fill) and live actionability all anchor 1R on ``ceiling - stop``, so
    # anchoring here keeps realized_r and the measured-move fallback honest -- not measured
    # from a midpoint that is never traded.
    reference = ceiling
    risk = reference - stop
    if floor >= ceiling or risk <= 0:
        return None

    target = ctx.reversal_low + cfg.reversal_retrace_frac * (ctx.decline_high - ctx.reversal_low)
    if target <= ceiling:  # retrace already below the entry -> a modest measured move
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
    confirm_lag: int = 0    # bars from flip to confirmation (0 = early / next-bar = 1)


def reversal_conviction_tier(volume_ratio: float, is_spring: bool, strength: str | None,
                             cfg: StrategyConfig) -> str:
    """Classify a reversal's conviction for tiered surfacing + sizing. The replay found the
    two strongest, ADDITIVE filters are a high-volume bounce and a Wyckoff spring:
      premium = high-volume bounce AND spring (+0.25R net, the edge);
      strong  = any single conviction signal (high volume, spring, or confirmed follow-through);
      base    = none.
    """
    high_vol = volume_ratio >= cfg.reversal_premium_min_rvol
    if high_vol and is_spring:
        return "premium"
    if high_vol or is_spring or strength == "confirmed":
        return "strong"
    return "base"


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_reversal(s: ReversalScoreInputs, cfg: StrategyConfig | None = None) -> float:
    """0..1 conviction, driven by the components that SEPARATE outcomes.

    The 2026-07-03 rank sweep over the fixed confirmed replay book found exactly one
    ordering signal with a positive clustered separation bound at both cost levels:
    CONFIRMATION LAG (a flip that paused before confirming beats a one-bar rip), with
    flip volume adding a little. The legacy HA-quality/RSI-depth-heavy vector -- and
    every individual component -- did not separate. The default weights encode that
    verdict; all weights live in ``cfg`` (``reversal_score_w_*``, normalized by their
    sum so only ratios matter) so the replay machinery can re-sweep them."""
    cfg = cfg or _DEFAULT_CFG
    lag = _clip01(s.confirm_lag / max(cfg.reversal_confirm_window, 1))
    bounce = 0.6 * _clip01(s.body_frac) + 0.4 * (1.0 if s.shaved_bottom else 0.0)  # HA flip
    downtrend = _clip01(s.red_run / max(s.decline_bars, 1))                        # HA red run
    volume = _clip01(s.volume_ratio - 1.0)        # 2x average volume saturates
    confirmation = 1.0 if s.confirmed else 0.0
    depth = _clip01((s.rsi_floor - s.min_rsi) / s.rsi_floor) if s.rsi_floor > 0 else 0.0
    weights = (cfg.reversal_score_w_lag, cfg.reversal_score_w_volume,
               cfg.reversal_score_w_bounce, cfg.reversal_score_w_downtrend,
               cfg.reversal_score_w_confirmed, cfg.reversal_score_w_depth)
    total = sum(weights)
    if total <= 0:
        return 0.0
    parts = (lag, volume, bounce, downtrend, confirmation, depth)
    return _clip01(sum(w * p for w, p in zip(weights, parts)) / total)

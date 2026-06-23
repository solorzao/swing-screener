"""Tests for the reversal-play detector, levels, and scoring.

Frames are built through the real ``build_frame`` (HA + EMAs + RSI + ATR +
classification) from synthetic OHLCV so the detector sees exactly what it sees
in production.
"""

import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.signals.frame import build_frame
from swing_screener.signals.reversal import (
    CONFIRMED,
    EARLY,
    ReversalContext,
    ReversalScoreInputs,
    compute_reversal_zone,
    detect_reversal,
    score_reversal,
)

CFG = StrategyConfig()


def _frame(rows):
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="D")
    df = pd.DataFrame(rows, index=idx)
    if "volume" not in df.columns:
        df["volume"] = 1_000_000.0
    return build_frame(df[["open", "high", "low", "close", "volume"]].astype(float), CFG)


def _bar(o, h, low, c, v=1_000_000.0):
    return {"open": o, "high": h, "low": low, "close": c, "volume": v}


def _reversal_rows(*, confirm=False, bounce=True):
    """A flat base, a steep RSI-crushing decline below the slow EMA, then a green
    HA flip (and optionally a follow-through bar)."""
    rows, p = [], 100.0
    for i in range(46):  # flat base: EMAs ~100, RSI ~50
        o = p
        c = p + (0.5 if i % 2 else -0.5)
        rows.append(_bar(o, max(o, c) + 0.3, min(o, c) - 0.3, c))
        p = c
    for _ in range(14):  # capitulation: RSI << 25, price well below the slow EMA
        o = p
        c = p - 2.2
        rows.append(_bar(o, o + 0.2, c - 0.3, c))
        p = c
    if not bounce:
        rows.append(_bar(p, p + 0.2, p - 2.3, p - 2.0))  # another red bar -- no life
        return rows
    # strong green bounce on heavy volume -- big enough to flip the (smoothed) HA
    # candle green in one bar.
    o = p + 0.2
    c = p + 12.0
    h = c + 0.5
    rows.append(_bar(o, h, p - 0.1, c, v=3_000_000.0))
    if confirm:  # follow-through closes above the bounce bar's high
        rows.append(_bar(c, h + 3.0, c - 0.3, h + 2.5, v=2_500_000.0))
    return rows


def test_detect_early_reversal_on_fresh_flip():
    ctx = detect_reversal(_frame(_reversal_rows()), CFG)
    assert ctx is not None
    assert ctx.strength == EARLY
    assert ctx.min_rsi < CFG.reversal_oversold_rsi_max          # genuinely capitulated
    assert ctx.reversal_low < ctx.ema_slow                      # beaten below the mean
    assert ctx.volume_ratio > 1.0                               # bounce on above-avg volume


def test_detect_confirmed_reversal_on_follow_through():
    ctx = detect_reversal(_frame(_reversal_rows(confirm=True)), CFG)
    assert ctx is not None
    assert ctx.strength == CONFIRMED


def test_no_reversal_without_a_green_flip():
    assert detect_reversal(_frame(_reversal_rows(bounce=False)), CFG) is None


def test_reversal_is_ha_gated_not_rsi_gated():
    # Same HA structure (downtrend -> flip below the slow EMA), but force RSI well
    # above any 'oversold' level. It must STILL detect -- proving the gate is HA
    # structure, not RSI (the old hard RSI<25 gate would have rejected this).
    f = _frame(_reversal_rows()).copy()
    f["rsi"] = 60.0
    ctx = detect_reversal(f, CFG)
    assert ctx is not None
    assert ctx.red_run >= CFG.reversal_min_bearish_bars
    assert ctx.min_rsi >= CFG.reversal_oversold_rsi_max  # 60 -> not oversold, yet detected


def _shallow_dip_rows():
    """A rising base, then a sharp 2-bar dip below the EMA + a flip -- NOT a sustained
    HA downtrend, so the red-run gate should reject it."""
    rows, p = [], 100.0
    for _ in range(50):
        o = p
        c = p + 0.8
        rows.append(_bar(o, c + 0.2, o - 0.2, c))
        p = c
    for _ in range(2):  # a 2-bar dip, not a downtrend
        o = p
        c = p - 13.0
        rows.append(_bar(o, o + 0.2, c - 0.3, c))
        p = c
    rows.append(_bar(p + 0.2, p + 14.0, p - 0.1, p + 13.5, v=3_000_000.0))
    return rows


def test_two_bar_dip_is_not_a_reversal():
    assert detect_reversal(_frame(_shallow_dip_rows()), CFG) is None  # red-run gate rejects it


def test_no_reversal_in_a_healthy_uptrend():
    # a steady grind up is never oversold -> no reversal candidate
    rows, p = [], 50.0
    for _ in range(70):
        o = p
        c = p + 0.8
        rows.append(_bar(o, c + 0.2, o - 0.2, c))
        p = c
    assert detect_reversal(_frame(rows), CFG) is None


def test_reversal_zone_levels_are_ordered():
    ctx = detect_reversal(_frame(_reversal_rows()), CFG)
    zone = compute_reversal_zone(ctx, CFG)
    assert zone is not None
    assert zone.stop < zone.floor <= zone.ceiling < zone.target  # stop below, target above
    assert zone.risk > 0


def test_reversal_target_is_a_decline_retracement():
    # target = a deep retracement of the prior decline toward the breakdown level.
    ctx = detect_reversal(_frame(_reversal_rows()), CFG)
    zone = compute_reversal_zone(ctx, CFG)
    retrace = ctx.reversal_low + CFG.reversal_retrace_frac * (ctx.decline_high - ctx.reversal_low)
    assert zone.target > zone.ceiling
    assert abs(zone.target - retrace) < 1e-6


def test_reversal_pullback_entry_gives_sane_rr():
    # A confirmed bounce from 100 -> 110 (prior breakdown high 118). The entry is a
    # PULLBACK into the bounce (below 110), the stop is below the bounce origin (100),
    # and the target retraces toward 118 -> reward:risk is sane, not the old ~0.14.
    ctx = ReversalContext(
        trigger_ts=pd.Timestamp("2026-06-15"), trigger_close=110.0, atr=2.0,
        reversal_low=100.0, bounce_high=110.0, rsi=42.0, min_rsi=22.0,
        strength=CONFIRMED, body_frac=0.5, shaved_bottom=True, red_run=4, decline_bars=6,
        volume_ratio=1.5, ema_slow=108.0, decline_high=118.0,
    )
    zone = compute_reversal_zone(ctx, CFG)
    assert zone.ceiling < ctx.bounce_high      # entry is a pullback, NOT the chase price
    assert zone.floor < zone.ceiling
    assert zone.stop < ctx.reversal_low        # stop below the bounce origin
    assert zone.target > zone.ceiling
    reward_to_risk = (zone.target - zone.ceiling) / (zone.ceiling - zone.stop)
    assert reward_to_risk > 1.0                # sane geometry, not the old wide-stop 0.14


def _rev_ctx(**over):
    base = dict(
        trigger_ts=pd.Timestamp("2026-06-15"), trigger_close=110.0, atr=2.0,
        reversal_low=100.0, bounce_high=110.0, rsi=42.0, min_rsi=22.0,
        strength=CONFIRMED, body_frac=0.5, shaved_bottom=True, red_run=4, decline_bars=6,
        volume_ratio=1.5, ema_slow=108.0, decline_high=118.0,
    )
    base.update(over)
    return ReversalContext(**base)


def test_reversal_fallback_target_meets_r_multiple_from_ceiling():
    """When the structural retrace sits below the entry, the measured-move fallback must
    deliver reversal_target_r_multiple R measured from the entry CEILING (the fill/size
    anchor), not from the midpoint reference."""
    cfg = StrategyConfig()
    # decline_high 105 puts the 0.786 retrace (~103.9) below the ceiling (~106.2), so the
    # measured-move fallback governs the target.
    ctx = _rev_ctx(reversal_low=100.0, bounce_high=110.0, atr=4.0, decline_high=105.0)
    z = compute_reversal_zone(ctx, cfg)
    assert z is not None
    assert z.target > z.ceiling  # fallback used
    realized_r = (z.target - z.ceiling) / (z.ceiling - z.stop)
    assert abs(realized_r - cfg.reversal_target_r_multiple) < 1e-9


def test_reversal_risk_is_anchored_on_the_ceiling():
    """zone.risk -- the shadow book's 1R denominator -- must be ceiling - stop, the same
    anchor the fill, sizing and actionability use, so realized_r is measured honestly."""
    cfg = StrategyConfig()
    ctx = _rev_ctx(reversal_low=100.0, bounce_high=110.0, atr=2.0, decline_high=118.0)
    z = compute_reversal_zone(ctx, cfg)
    assert z is not None
    assert z.risk == z.ceiling - z.stop


def _score_inputs(**over):
    base = dict(body_frac=0.4, shaved_bottom=False, red_run=3, decline_bars=6,
                volume_ratio=1.0, confirmed=False, min_rsi=20.0, rsi_floor=25.0)
    base.update(over)
    return ReversalScoreInputs(**base)


def test_score_is_ha_centric():
    b = score_reversal(_score_inputs())
    assert 0.0 <= b <= 1.0
    # HA factors dominate: a stronger flip body and a longer red run both raise it.
    assert score_reversal(_score_inputs(body_frac=0.9)) > b      # stronger HA flip
    assert score_reversal(_score_inputs(red_run=6)) > b          # deeper HA downtrend
    assert score_reversal(_score_inputs(shaved_bottom=True)) > b  # clean HA flip
    assert score_reversal(_score_inputs(volume_ratio=2.0)) > b   # heavier volume
    assert score_reversal(_score_inputs(confirmed=True)) > b     # confirmation
    assert score_reversal(_score_inputs(min_rsi=5.0)) > b        # deeper oversold (a confirm)


def test_rsi_depth_is_a_minor_factor_vs_ha():
    # The HA flip+downtrend swing must outweigh the RSI-depth swing, or the screener
    # would be RSI-driven. Max-out HA factors vs max-out RSI depth from the same base.
    ha_max = score_reversal(_score_inputs(body_frac=1.0, shaved_bottom=True, red_run=6))
    rsi_max = score_reversal(_score_inputs(min_rsi=0.0))
    assert ha_max > rsi_max

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


def test_reversal_target_is_overhead_resistance():
    # target should be a real overhead level (EMA reclaim / swing high) above the
    # ceiling -- a mean-reversion level, not an arbitrary multiple.
    ctx = detect_reversal(_frame(_reversal_rows()), CFG)
    zone = compute_reversal_zone(ctx, CFG)
    assert zone.target > zone.ceiling
    assert zone.target in (ctx.ema_slow, ctx.swing_high)


def test_score_rewards_depth_volume_and_confirmation():
    base = ReversalScoreInputs(min_rsi=20.0, rsi_floor=25.0, body_frac=0.4,
                               shaved_bottom=False, volume_ratio=1.0, confirmed=False)
    deeper = ReversalScoreInputs(min_rsi=5.0, rsi_floor=25.0, body_frac=0.4,
                                 shaved_bottom=False, volume_ratio=1.0, confirmed=False)
    louder = ReversalScoreInputs(min_rsi=20.0, rsi_floor=25.0, body_frac=0.4,
                                 shaved_bottom=False, volume_ratio=2.0, confirmed=False)
    confirmed = ReversalScoreInputs(min_rsi=20.0, rsi_floor=25.0, body_frac=0.4,
                                    shaved_bottom=False, volume_ratio=1.0, confirmed=True)
    b = score_reversal(base)
    assert 0.0 <= b <= 1.0
    assert score_reversal(deeper) > b      # deeper capitulation scores higher
    assert score_reversal(louder) > b      # heavier volume scores higher
    assert score_reversal(confirmed) > b   # confirmation scores higher

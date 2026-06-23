from swing_screener.config import StrategyConfig
from swing_screener.signals.entry_zone import compute_zone, nearest_resistance


def test_zone_ordering_and_risk():
    cfg = StrategyConfig()
    z = compute_zone(trigger_close=100.0, atr=4.0, swing_low=96.0, cfg=cfg)
    assert z is not None
    assert z.floor < z.ceiling
    assert z.stop < z.floor
    assert z.ceiling == 100.0 + cfg.ceiling_atr_mult * 4.0
    assert z.target > z.ceiling
    assert z.risk > 0
    # R is anchored on the entry ceiling (the fill): reference == ceiling and
    # risk == ceiling - stop. Here the measured-move fallback (ceiling + 2*atr) is only
    # 1.25R, below the 1.5R floor, so the target is pushed to ceiling + min_target_r*risk.
    assert z.reference == z.ceiling
    assert z.risk == z.ceiling - z.stop
    assert z.target == z.ceiling + cfg.min_target_r * z.risk


def test_target_floor_anchors_on_ceiling_the_real_fill():
    """The min_target_r floor must hold measured from the CEILING -- the price the
    order fills and sizes at (insight.size_order, fill.resolve_fill, actionability) --
    not from the midpoint reference. Before the fix the floor was anchored on the
    midpoint, so the realized R:R at the actual fill fell well below min_target_r
    (~0.86R for this setup advertised as >=1.5R)."""
    cfg = StrategyConfig()
    z = compute_zone(trigger_close=100.0, atr=4.0, swing_low=96.0, cfg=cfg)
    assert z is not None
    realized_rr = (z.target - z.ceiling) / (z.ceiling - z.stop)
    assert realized_rr >= cfg.min_target_r


def test_inverted_zone_returns_none():
    # Shallow pullback (swing_low above trigger_close) + large ATR pushes the
    # floor above the ceiling: floor = 101.2 > ceiling = 100.7 at defaults.
    cfg = StrategyConfig()
    z = compute_zone(trigger_close=100.0, atr=2.0, swing_low=101.0, cfg=cfg)
    assert z is None


def test_non_positive_risk_returns_none():
    # Stop placed above the entry ceiling yields risk = ceiling - stop <= 0 while
    # floor < ceiling, isolating the risk guard from the inversion guard. A strongly
    # negative stop buffer lifts the stop (116.0) above the ceiling (103.5).
    cfg = StrategyConfig(stop_buffer_atr=-2.0)
    z = compute_zone(trigger_close=100.0, atr=10.0, swing_low=96.0, cfg=cfg)
    assert z is None


# --- nearest_resistance (pure pivot scan) ----------------------------------

def test_nearest_resistance_returns_pivot_above():
    # A clear swing-high pivot at index 3 (value 110) tops width=2 bars each side.
    highs = [100.0, 101.0, 102.0, 110.0, 103.0, 102.5, 101.0]
    assert nearest_resistance(highs, above=104.0, width=2) == 110.0


def test_nearest_resistance_none_when_no_pivot_above():
    # The only pivot (110 at index 3) sits below the `above` threshold.
    highs = [100.0, 101.0, 102.0, 110.0, 103.0, 102.5, 101.0]
    assert nearest_resistance(highs, above=120.0, width=2) is None


def test_nearest_resistance_picks_lowest_qualifying_pivot():
    # Two pivots above the threshold (105 at idx 3, 112 at idx 7); the closest
    # overhead (lowest) wins.
    highs = [100.0, 101.0, 102.0, 105.0, 101.0, 100.0, 103.0, 112.0, 104.0, 103.5]
    assert nearest_resistance(highs, above=104.0, width=2) == 105.0


def test_nearest_resistance_ignores_pivot_not_tall_enough():
    # 106 at index 2 only tops 1 bar on each side; the bar 2 to its left (108)
    # is higher, so with width=2 it is NOT a pivot. With width=1 it qualifies.
    highs = [108.0, 101.0, 106.0, 105.0, 104.0, 103.0]
    assert nearest_resistance(highs, above=104.5, width=2) is None
    assert nearest_resistance(highs, above=104.5, width=1) == 106.0


# --- cascade: resistance -> ATR fallback -> 1.5R floor ---------------------

def test_target_uses_nearest_resistance_when_overhead():
    cfg = StrategyConfig()
    # zone at defaults: ceiling = 100 + 0.35*4 = 101.4, reference == ceiling, risk = 6.4.
    # A pivot must clear the 1.5R floor (ceiling + 1.5*6.4 = 111.0) to be used directly,
    # so the qualifying pivot sits at 112.0 (index 3).
    highs = [99.0, 100.0, 101.0, 112.0, 102.0, 101.5, 100.0]
    z = compute_zone(100.0, 4.0, 96.0, cfg, recent_highs=highs)
    assert z is not None
    # (112.0 - 101.4)/6.4 = 10.6/6.4 = 1.66 >= 1.5 -> resistance used directly.
    assert z.target == 112.0


def test_target_atr_fallback_when_no_resistance():
    cfg = StrategyConfig()
    # No qualifying pivot above the ceiling -> measured-move fallback. A shallow pullback
    # (swing_low 98) keeps risk small (4.4) so the fallback (ceiling + 2*atr) is 1.82R,
    # above the 1.5R floor, so the fallback governs.
    highs = [99.0, 100.0, 100.5, 100.2, 100.0, 99.5, 99.0]
    z = compute_zone(100.0, 4.0, 98.0, cfg, recent_highs=highs)
    assert z is not None
    # ceiling + target_atr_mult * atr = 101.4 + 2.0*4.0 = 109.4.
    assert z.target == z.ceiling + cfg.target_atr_mult * 4.0


def test_target_pushed_to_min_r_floor_when_resistance_too_close():
    cfg = StrategyConfig()
    # A pivot at 102.0 sits just above the ceiling (101.4) but its R:R from the ceiling
    # is only (102.0 - 101.4)/6.4 = 0.09 < 1.5, so the target is pushed to the 1.5R floor.
    highs = [99.0, 100.0, 101.0, 102.0, 101.2, 100.5, 100.0]
    z = compute_zone(100.0, 4.0, 96.0, cfg, recent_highs=highs)
    assert z is not None
    # ceiling + min_target_r * risk = 101.4 + 1.5*6.4 = 111.0.
    assert z.target == z.ceiling + cfg.min_target_r * z.risk


def test_compute_zone_without_recent_highs_still_valid():
    cfg = StrategyConfig()
    # Backward-compatible: omitting recent_highs takes the ATR-fallback path (which here,
    # with a shallow pullback, governs above the 1.5R floor).
    z = compute_zone(100.0, 4.0, 98.0, cfg)
    assert z is not None
    assert z.target == z.ceiling + cfg.target_atr_mult * 4.0

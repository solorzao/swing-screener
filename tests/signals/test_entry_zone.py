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
    # No recent_highs -> measured-move ATR fallback: reference + target_atr_mult * atr.
    # reference = (96.4 + 101.4)/2 = 98.9; target = 98.9 + 2.0*4.0 = 106.9.
    assert z.reference == 98.9
    assert z.risk == 98.9 - 95.0
    assert z.target == 98.9 + cfg.target_atr_mult * 4.0


def test_inverted_zone_returns_none():
    # Shallow pullback (swing_low above trigger_close) + large ATR pushes the
    # floor above the ceiling: floor = 101.2 > ceiling = 100.7 at defaults.
    cfg = StrategyConfig()
    z = compute_zone(trigger_close=100.0, atr=2.0, swing_low=101.0, cfg=cfg)
    assert z is None


def test_non_positive_risk_returns_none():
    # Stop placed above the zone reference yields risk <= 0 while floor < ceiling,
    # isolating the risk guard from the inversion guard.
    cfg = StrategyConfig(stop_buffer_atr=-0.5)
    z = compute_zone(trigger_close=104.0, atr=10.0, swing_low=100.0, cfg=cfg)
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
    # zone at defaults: ceiling = 100 + 0.35*4 = 101.4, reference = 98.9, risk = 3.9.
    # A pivot at 104.0 (index 3) sits just above the ceiling; 104.0 is the target.
    # R:R = (104.0 - 98.9)/3.9 = 5.1/3.9 = 1.31 ... that's BELOW the 1.5R floor,
    # so push the resistance higher to keep this case on the resistance path.
    highs = [99.0, 100.0, 101.0, 108.0, 102.0, 101.5, 100.0]
    z = compute_zone(100.0, 4.0, 96.0, cfg, recent_highs=highs)
    assert z is not None
    # (108.0 - 98.9)/3.9 = 9.1/3.9 = 2.33 >= 1.5 -> resistance used directly.
    assert z.target == 108.0


def test_target_atr_fallback_when_no_resistance():
    cfg = StrategyConfig()
    # No qualifying pivot above the ceiling -> measured-move fallback.
    highs = [99.0, 100.0, 100.5, 100.2, 100.0, 99.5, 99.0]
    z = compute_zone(100.0, 4.0, 96.0, cfg, recent_highs=highs)
    assert z is not None
    # reference + target_atr_mult * atr = 98.9 + 2.0*4.0 = 106.9.
    assert z.target == 98.9 + cfg.target_atr_mult * 4.0


def test_target_pushed_to_min_r_floor_when_resistance_too_close():
    cfg = StrategyConfig()
    # A pivot at 102.0 sits just above the ceiling (101.4) but its R:R is only
    # (102.0 - 98.9)/3.9 = 0.79 < 1.5, so the target is pushed to the 1.5R floor.
    highs = [99.0, 100.0, 101.0, 102.0, 101.2, 100.5, 100.0]
    z = compute_zone(100.0, 4.0, 96.0, cfg, recent_highs=highs)
    assert z is not None
    # reference + min_target_r * risk = 98.9 + 1.5*3.9 = 104.75.
    assert z.target == 98.9 + cfg.min_target_r * z.risk


def test_compute_zone_without_recent_highs_still_valid():
    cfg = StrategyConfig()
    # Backward-compatible: omitting recent_highs takes the ATR-fallback path.
    z = compute_zone(100.0, 4.0, 96.0, cfg)
    assert z is not None
    assert z.target == 98.9 + cfg.target_atr_mult * 4.0

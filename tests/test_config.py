from swing_screener.config import StrategyConfig


def test_defaults_present():
    cfg = StrategyConfig()
    assert cfg.ema_fast == 20
    assert cfg.ema_slow == 50
    assert cfg.max_pullback_bars >= 1
    assert 0 < cfg.zone_body_frac < 1
    assert cfg.max_hold_bars["4h"] > 0


def test_override():
    cfg = StrategyConfig(ema_fast=10)
    assert cfg.ema_fast == 10


def test_live_book_is_net_of_costs_by_default():
    """fill_slippage_atr must default to the a-priori 0.05 ATR haircut: with 0.0 the live
    shadow book (the evidence base for reflection/calibration) and the scheduled optimizer
    judged everything GROSS of costs while reflect.py claimed cost-parity (2026-07 audit).
    Fixed a-priori from a microstructure rule -- never swept by the optimizer."""
    assert StrategyConfig().fill_slippage_atr == 0.05


def test_reversal_fill_window_defaults_to_multi_bar():
    """A reversal entry is a resting limit into a pullback; giving it one bar to fill made
    ~95% of confirmed reversals unfillable and adversely selected the rest (live -0.86R vs
    replay +0.125R, 2026-07 audit). The shadow book measures a multi-bar resting order."""
    assert StrategyConfig().reversal_fill_window_bars == 5


def test_reversal_surfacing_defaults_to_confirmed_not_premium():
    """Pins the digest surfacing posture: the 503-validated CONFIRMED edge governs the
    reversal list; the premium tier (rare: ~0.2% of live signals) must NOT be the gate.
    premium_only=True silently starved the digest of reversal picks from 2026-06-28 on
    (premium overrides confirmed in reversal_picks), so this default is load-bearing."""
    cfg = StrategyConfig()
    assert cfg.reversal_surface_premium_only is False
    assert cfg.reversal_surface_confirmed_only is True


def test_continuation_surfacing_defaults_to_parked():
    """Pins the continuation PARKING posture: the 2026-07-25 ceiling_atr_mult sweep (Q6)
    completed the entry-economics decomposition with a NULL (best cell -0.103R, clustered
    lb -0.120 on the pinned 511-name corpus; docs/plans/2026-07-25-q6-q7-sweep-results.md)
    after selection, timing, ordering, and gates all graded no-edge -- so continuation
    must not surface (digest, cockpit picks, would_surface stamp) until a fundamentally
    different entry mechanic clears the replay bar. Detection/scoring/shadow-booking are
    deliberately unaffected (the registered forward books keep accruing)."""
    assert StrategyConfig().surface_continuation is False

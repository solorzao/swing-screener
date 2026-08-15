import pytest

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.variants import DEFAULT_VARIANT, build_screen_variants


def test_default_variant_is_the_base_config():
    base = StrategyConfig()
    variants = build_screen_variants(base)
    assert DEFAULT_VARIANT in variants
    assert variants[DEFAULT_VARIANT] is base


def test_includes_a_tighter_freshness_gate_variant():
    base = StrategyConfig(max_extension_atr=2.0)
    variants = build_screen_variants(base)
    alts = {n: c for n, c in variants.items() if n != DEFAULT_VARIANT}
    assert alts, "expected at least one alternative screen variant"
    # the shipped bake-off forward-tests a tighter gate than the base
    assert any(c.max_extension_atr < base.max_extension_atr for c in alts.values())


def test_variants_share_base_indicator_periods():
    # every variant must keep the base's indicator periods (frames are reused)
    base = StrategyConfig()
    for name, cfg in build_screen_variants(base).items():
        assert cfg.ema_fast == base.ema_fast and cfg.atr_period == base.atr_period, name


def test_rejects_a_variant_that_changes_an_indicator():
    # a variant that retunes an indicator period would score against the wrong frame;
    # the guard must refuse it rather than silently mis-measure.
    from dataclasses import replace

    from swing_screener.pipeline.variants import _assert_shared_indicators

    base = StrategyConfig()
    with pytest.raises(ValueError, match="indicator field"):
        _assert_shared_indicators(base, "bad", replace(base, ema_fast=10))


def test_shadow_tracks_continuation_volband_combo():
    # the volume+depth+body continuation combo (edge-tournament best) is shadow-tracked
    # forward as a screen variant: booked under the baseline exit, measured, never surfaced.
    base = StrategyConfig()
    variants = build_screen_variants(base)
    assert "cont_volband" in variants
    v = variants["cont_volband"]
    assert v.vol_thrust_min >= 1.3 and v.require_value_band and v.min_trigger_body_frac >= 0.5


def test_shadow_tracks_reversal_highvol_combo():
    # the high-bounce-volume reversal gate (edge-discovery exp 5) is a cost-robust edge
    # (+0.11R, 95%low +0.03 net of slippage); shadow-track it forward as a screen variant.
    base = StrategyConfig()
    variants = build_screen_variants(base)
    assert "rev_highvol" in variants
    assert variants["rev_highvol"].reversal_min_flip_rvol >= 1.3


def test_legacy_counterfactual_variants_are_retired():
    """`rev_confirm1` and `rev_retrace786` existed to second-guess the two 2026-07-03
    flips. Both LOST to the default on the live forward book and were retired
    2026-08-15, so the roster must ship the flipped defaults with no counterfactual
    book -- and a re-add needs a fresh registry row, not a quiet roster line."""
    base = StrategyConfig()
    variants = build_screen_variants(base)
    assert "rev_confirm1" not in variants and "rev_retrace786" not in variants
    # the defaults those books challenged, still in force
    assert base.reversal_confirm_window == 3
    assert base.reversal_retrace_frac == 1.0


def test_detection_only_fields_are_allowed_in_variants():
    # the outside-bar trigger and entry-depth gate are DETECTION fields (computed from the
    # shared frame), not indicator periods -- a variant may set them without rebuilding frames.
    from dataclasses import replace

    from swing_screener.pipeline.variants import _assert_shared_indicators

    base = StrategyConfig()
    _assert_shared_indicators(base, "outside_bar", replace(base, trigger_kind="outside_bar"))
    _assert_shared_indicators(base, "band_touch", replace(base, require_band_touch=True))


def test_rejects_a_variant_that_changes_an_exit_knob():
    """A screen variant differing in an EXIT field is a silent no-op (open trades advance
    under the BASE config's exits in both live and replay), so the guard must fail loudly
    and point at arms instead of letting a false null be recorded (2026-07 review)."""
    from dataclasses import replace

    from swing_screener.pipeline.variants import _assert_shared_indicators

    base = StrategyConfig()
    for field, value in (("partial_frac", 0.33), ("trail_mode", "chandelier"),
                         ("time_stop_factor", base.time_stop_factor * 2),
                         ("reversal_momentum_flip_exit", True),
                         ("fill_slippage_atr", base.fill_slippage_atr + 0.05)):
        bad = replace(base, **{field: value})
        with pytest.raises(ValueError, match="exit field"):
            _assert_shared_indicators(base, f"bad_{field}", bad)

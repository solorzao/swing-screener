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


def test_detection_only_fields_are_allowed_in_variants():
    # the outside-bar trigger and entry-depth gate are DETECTION fields (computed from the
    # shared frame), not indicator periods -- a variant may set them without rebuilding frames.
    from dataclasses import replace

    from swing_screener.pipeline.variants import _assert_shared_indicators

    base = StrategyConfig()
    _assert_shared_indicators(base, "outside_bar", replace(base, trigger_kind="outside_bar"))
    _assert_shared_indicators(base, "band_touch", replace(base, require_band_touch=True))

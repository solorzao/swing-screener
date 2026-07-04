from swing_screener.config import StrategyConfig
from swing_screener.pipeline.arms import BASELINE, build_arms


def test_baseline_arm_is_all_or_nothing_with_flip_on():
    arms = build_arms(StrategyConfig())
    assert arms[BASELINE].partial_frac == 0.0
    assert arms[BASELINE].momentum_flip_exit is True


def test_no_flip_arm_disables_only_the_momentum_flip():
    """The no_flip arm mirrors baseline (all-or-nothing) and differs ONLY in the
    momentum_flip exit, so breakdown(trades, "arm") is a clean same-sample A/B of the flip."""
    arms = build_arms(StrategyConfig())
    assert "no_flip" in arms
    nf = arms["no_flip"]
    assert nf.momentum_flip_exit is False
    assert nf.partial_frac == 0.0  # identical entry/partial economics to baseline


def test_be_1r_arm_is_registered_with_the_breakeven_knob():
    from swing_screener.config import StrategyConfig
    from swing_screener.pipeline.arms import build_arms

    arms = build_arms(StrategyConfig())
    be = arms["be_1r"]
    assert be.breakeven_after_r == 1.0
    assert be.partial_frac == 0.0        # all-or-nothing apart from the ratchet
    assert arms["baseline"].breakeven_after_r == 0.0

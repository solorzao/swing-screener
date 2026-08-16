"""Tests for the conviction-FLOOR helpers (``pipeline.insight``).

The floor is what lets the digest surface only medium/high plays AND skip the paid
conviction call on picks that could never clear it. Both halves read the same ladder
helpers here, so the spend gate and the display filter cannot drift apart.

The key asymmetry these lock down: ``conviction_baseline`` never returns "low" (it
returns avoid/medium/high), so under the hard +-1 clamp an "avoid" baseline can only
ever land on avoid/low -- it can NEVER reach medium. That is what makes skipping its
call provably lossless. A play type that CERTIFIES for +-2 changes that answer, and the
helper must follow the earned step rather than a hard-coded assumption.
"""

import pytest

from swing_screener.pipeline.insight import (
    best_reachable_conviction,
    meets_conviction_floor,
)


# ---------------------------------------------------------------------------
# best_reachable_conviction: the highest grade a baseline could reach.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("baseline", "max_step", "expected"),
    [
        # The hard +-1 clamp (every play type today).
        ("avoid", 1, "low"),      # THE case the gate keys on: cannot reach medium
        ("medium", 1, "high"),
        ("high", 1, "high"),      # saturates at the top of the ladder
        # An EARNED +-2 nudge (a play type whose conviction calibrates).
        ("avoid", 2, "medium"),   # now reachable -> the gate must let the call through
        ("medium", 2, "high"),
        ("high", 2, "high"),
    ],
)
def test_best_reachable_conviction(baseline: str, max_step: int, expected: str) -> None:
    assert best_reachable_conviction(baseline, max_step) == expected


# ---------------------------------------------------------------------------
# meets_conviction_floor: the display filter's predicate.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("conviction", "floor", "expected"),
    [
        ("high", "medium", True),
        ("medium", "medium", True),    # the floor itself passes (inclusive)
        ("low", "medium", False),
        ("avoid", "medium", False),
        ("low", "low", True),          # floor="low" restores today's behavior
        ("avoid", "low", False),
        ("avoid", "avoid", True),      # floor="avoid" surfaces everything graded
    ],
)
def test_meets_conviction_floor(conviction: str, floor: str, expected: bool) -> None:
    assert meets_conviction_floor(conviction, floor) is expected


def test_ungraded_pick_never_meets_the_floor() -> None:
    """A pick with NO conviction (deep analysis off, playbook missing, spend ceiling
    hit, or beyond top-N) must be dropped, not fail open. Surfacing an ungraded pick
    is exactly the low-conviction noise the floor exists to remove."""
    assert meets_conviction_floor(None, "medium") is False


def test_ungraded_pick_is_dropped_even_by_the_lowest_floor() -> None:
    """Even floor="avoid" -- which admits every GRADED conviction -- must not admit an
    ungraded pick: absent is not a grade."""
    assert meets_conviction_floor(None, "avoid") is False


# ---------------------------------------------------------------------------
# The empty-string sentinel: the true OFF switch.
# ---------------------------------------------------------------------------
def test_empty_floor_disables_the_filter_entirely() -> None:
    """No floor on the ladder can restore the pre-floor behavior, because EVERY floor
    drops ungraded picks and the old digest surfaced them. So "" is the explicit off
    switch -- it admits everything, graded or not."""
    assert meets_conviction_floor(None, "") is True
    for grade in ("avoid", "low", "medium", "high"):
        assert meets_conviction_floor(grade, "") is True


def test_default_floor_is_a_valid_rung_on_the_ladder() -> None:
    """Guards a typo in the shipped config: an unknown floor would raise deep inside a
    live digest run, and the ladder lookup is the only thing that would catch it."""
    from swing_screener.config import StrategyConfig
    from swing_screener.pipeline.insight import _CONVICTIONS

    floor = StrategyConfig().min_conviction
    assert floor == "medium"
    assert floor in _CONVICTIONS

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.arms import (
    BASELINE,
    build_arms,
    build_draining_arms,
    build_stepping_arms,
)
from swing_screener.pipeline.registry import load_experiments
from tests.pipeline.test_registry import REPO_EDGE

# Settled futile against baseline on 2026-08-15 and removed from the roster; the
# registry keeps their numbers. Named here so a re-add without a fresh registration is
# a test failure, not a silent resurrection of a decided question.
RETIRED_ARMS = ("no_flip", "partial33_cond", "partial33_chand", "be_1r")


def test_baseline_arm_is_all_or_nothing_with_flip_on():
    arms = build_arms(StrategyConfig())
    assert arms[BASELINE].partial_frac == 0.0
    assert arms[BASELINE].momentum_flip_exit is True


def test_roster_is_baseline_only_after_the_exit_arms_settled():
    """All four exit-policy arms settled futile, so the shadow book books ONE row per
    fill per screen variant instead of five -- the accrual-dilution relief that made the
    retirement worth doing. A new arm belongs here only with a new registry row."""
    arms = build_arms(StrategyConfig())
    assert set(arms) == {BASELINE}
    for name in RETIRED_ARMS:
        assert name not in arms


def test_retired_arms_keep_their_audit_record():
    """Retirement deletes the roster line but NEVER the registry row: the decision text
    and decided_at are the audit half of a settlement (docs/cockpit.md 'To retire')."""
    rows = {e.name: e for e in load_experiments(REPO_EDGE)}
    for name in RETIRED_ARMS:
        row = rows[name]
        assert row.status == "retired"
        assert row.decided_at == "2026-08-15"
        assert row.decision and "FUTILE" in row.decision


def test_draining_arms_are_stepped_but_never_opened():
    """The whole point of the split: a draining arm books no new fills (absent from the
    OPENING roster) yet the stepper can still advance the ones it has (present in the
    stepping set). Collapsing these two sets either strands trades or keeps accruing
    them."""
    base = StrategyConfig()
    opening, draining = build_arms(base), build_draining_arms(base)
    assert not (set(opening) & set(draining)), "an arm cannot both open and drain"
    assert set(build_stepping_arms(base)) == set(opening) | set(draining)
    # the stepping set must not shadow the opening configs
    for name, cfg in opening.items():
        assert build_stepping_arms(base)[name] == cfg


def test_draining_arms_keep_their_original_exit_policy():
    """A draining trade must advance under the policy it was BOOKED under. Advancing it
    under anything else rewrites an experiment that has already settled, which is worse
    than stranding it -- so these configs are pinned, not re-derived."""
    d = build_draining_arms(StrategyConfig())
    assert d["no_flip"].momentum_flip_exit is False
    assert d["no_flip"].partial_frac == 0.0
    assert d["partial33_cond"].partial_frac == 0.33
    assert d["partial33_cond"].partial_require_softening is True
    assert d["partial33_cond"].trail_mode == "breakeven"
    assert d["partial33_chand"].trail_mode == "chandelier"
    assert d["partial33_chand"].chandelier_atr_mult == 3.0
    assert d["partial33_chand"].partial_frac == 0.33
    assert d["be_1r"].breakeven_after_r == 1.0
    assert d["be_1r"].partial_frac == 0.0


def test_every_draining_arm_is_a_retired_experiment():
    """A name may only drain because it was RETIRED -- never as a way to keep an
    unregistered arm quietly running in the stepper."""
    rows = {e.name: e for e in load_experiments(REPO_EDGE)}
    for name in build_draining_arms(StrategyConfig()):
        assert name in rows, f"{name} drains without a registry row"
        assert rows[name].status == "retired", f"{name} drains while still active"

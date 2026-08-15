from swing_screener.config import StrategyConfig
from swing_screener.pipeline.arms import BASELINE, build_arms
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

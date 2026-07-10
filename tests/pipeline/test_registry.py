"""The experiment registry: the machine-readable pre-registration record.

Every forward experiment (exit arm or screen variant) has exactly one registry entry
carrying its hypothesis, stopping rule verbatim, MDE, and the git sha it was registered
under -- the solo pre-registration-theater mitigation. The registry and the code rosters
(pipeline/arms.py, pipeline/variants.py) must never drift: a roster entry without a
registry row (or vice versa) is a test failure, not a rendering surprise.
"""

from pathlib import Path

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.arms import BASELINE, build_arms
from swing_screener.pipeline.registry import Experiment, load_experiments
from swing_screener.pipeline.variants import DEFAULT_VARIANT, build_screen_variants

REPO_EDGE = Path(__file__).resolve().parents[2] / "edge"


def test_experiment_round_trip(tmp_path: Path) -> None:
    exp = Experiment(
        name="rev_highvol", kind="variant", play_type="reversal", control="default",
        hypothesis="h", stopping_rule="rule text", mde_r=0.10,
        target_ci_halfwidth_r=0.15, registered_at="2026-07-10",
        registered_sha="abc123", doc_ref="docs/x.md", provenance="roster comment",
        status="active",
    )
    (tmp_path / "experiments.json").write_text(
        "[" + exp.as_json() + "]", encoding="utf-8"
    )
    loaded = load_experiments(tmp_path)
    assert loaded == [exp]


def test_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_experiments(tmp_path) == []


def test_registry_matches_code_rosters() -> None:
    """Bidirectional lockstep with the real committed registry."""
    base = StrategyConfig()
    roster = (set(build_arms(base)) - {BASELINE}) | (
        set(build_screen_variants(base)) - {DEFAULT_VARIANT}
    )
    registered = {e.name for e in load_experiments(REPO_EDGE)}
    assert registered == roster

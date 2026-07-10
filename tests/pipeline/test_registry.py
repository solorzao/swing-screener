"""The experiment registry: the machine-readable pre-registration record.

Every forward experiment (exit arm or screen variant) has exactly one registry entry
carrying its hypothesis, stopping rule verbatim, MDE, and the git sha it was registered
under -- the solo pre-registration-theater mitigation. The registry and the code rosters
(pipeline/arms.py, pipeline/variants.py) must never drift: a roster entry without an
ACTIVE registry row (or vice versa) is a test failure, not a rendering surprise.
Retirement deletes the roster line but never the registry row -- the row is the audit
record, so a retired row must keep its decided_at/decision.
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
    """Bidirectional lockstep with the real committed registry, per kind.

    Only ACTIVE entries must match the rosters (retirement deletes the roster line, never
    the registry row). Kind matters: the settlement engine picks paired-vs-clustered delta
    machinery off ``kind``, so an arm registered as a variant (or vice versa) is exactly
    the dishonesty this test exists to catch.
    """
    base = StrategyConfig()
    exps = load_experiments(REPO_EDGE)
    assert len(exps) == len({e.name for e in exps})  # exactly one entry per experiment
    active = [e for e in exps if e.status == "active"]
    assert {e.name for e in active if e.kind == "arm"} == set(build_arms(base)) - {BASELINE}
    assert {e.name for e in active if e.kind == "variant"} == (
        set(build_screen_variants(base)) - {DEFAULT_VARIANT}
    )
    for exp in exps:
        if exp.status == "retired":
            assert exp.decided_at is not None, f"{exp.name}: retired without decided_at"
            assert exp.decision is not None, f"{exp.name}: retired without decision"

"""Tests for merging analyst-queued ProposedVariants into the optimizer sweep + the
multiple-comparisons (search-cost) accounting (Phase-6 Task 5).

North Star #1 (evidence gates promotion) + #2 (honest evidence): the analyst widens the
search by queueing candidate screen variants, and that widening must be PAID FOR.

  * The grid merge: a ``status=="queued"`` ProposedVariant joins the swept
    ``{name: StrategyConfig}`` grid (via ``to_config``); a non-queued one does NOT; one whose
    delta fails ``to_config`` validation is SKIPPED (logged), never poisoning the grid.
  * The MC accounting: the count of swept variants -- INCLUDING the analyst-queued ones --
    feeds the search-cost control. ``OptimizeResult.n_variants_tested`` is the true swept-grid
    size (configs *tested*, not just those that *traded*), and ``propose()``'s provenance
    reports it so a winner can't be cherry-picked from a silently-widened search.

Pure: no LLM, no network.
"""

import logging

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.optimize import OptimizeResult, build_config_grid
from swing_screener.pipeline.propose import _provenance
from swing_screener.pipeline.proposed import ProposedVariant


def _queued(name: str, delta: dict, *, status: str = "queued") -> ProposedVariant:
    return ProposedVariant(
        name=name, play_type="continuation", delta=delta,
        rationale="hunch", hunch_ref="continuation:x", status=status,
        drafted_at="2026-06-20", provenance="analyst",
    )


# =====================================================================================
# grid merge -- queued variants join the sweep; non-queued / invalid are excluded
# =====================================================================================
def test_queued_variant_is_merged_into_the_grid():
    base = StrategyConfig()
    pv = _queued("min_pb_2_q", {"min_pullback_bars": 2})
    grid = build_config_grid(base, proposed=[pv])
    assert "min_pb_2_q" in grid
    assert grid["min_pb_2_q"].min_pullback_bars == 2
    # the deterministic sweep points are still present
    assert "ext_2.0" in grid


def test_non_queued_variant_is_not_merged():
    base = StrategyConfig()
    pv = _queued("draft_q", {"min_pullback_bars": 2}, status="draft")
    grid = build_config_grid(base, proposed=[pv])
    assert "draft_q" not in grid


def test_invalid_delta_variant_is_skipped_not_in_grid(caplog):
    # An illegal (frozen-indicator) delta must NOT poison the grid; it is skipped + logged.
    base = StrategyConfig()
    good = _queued("good_q", {"min_pullback_bars": 2})
    bad = _queued("bad_q", {"ema_fast": 10})  # frozen indicator field -> to_config raises
    with caplog.at_level(logging.WARNING):
        grid = build_config_grid(base, proposed=[good, bad])
    assert "good_q" in grid
    assert "bad_q" not in grid
    assert any("bad_q" in r.message for r in caplog.records)


def test_unknown_key_variant_is_skipped_not_in_grid(caplog):
    base = StrategyConfig()
    bad = _queued("typo_q", {"not_a_knob": 1.0})
    with caplog.at_level(logging.WARNING):
        grid = build_config_grid(base, proposed=[bad])
    assert "typo_q" not in grid
    assert any("typo_q" in r.message for r in caplog.records)


def test_grid_without_proposed_is_unchanged():
    base = StrategyConfig()
    assert build_config_grid(base) == build_config_grid(base, proposed=[])
    assert build_config_grid(base, proposed=None) == build_config_grid(base)


# =====================================================================================
# MC accounting -- the swept-variant count (incl. queued) feeds the search-cost control
# =====================================================================================
def test_n_variants_tested_grows_by_the_queued_count():
    base = StrategyConfig()
    plain = build_config_grid(base)
    pvs = [
        _queued("a_q", {"min_pullback_bars": 2}),
        _queued("b_q", {"max_pullback_bars": 6}),
    ]
    widened = build_config_grid(base, proposed=pvs)
    # the swept grid grew by exactly the two valid queued variants
    assert len(widened) == len(plain) + 2


def test_optimize_result_reports_full_swept_grid_size():
    # n_variants_tested counts configs TESTED (the whole swept grid), not just those that
    # TRADED -- so a silently-widened search is visible even if the extra arms drew no trades.
    result = OptimizeResult(
        in_sample={"ext_2.0": _Dummy()},          # only one config actually traded
        out_of_sample={},
        winner="ext_2.0",
        n_variants_tested=6,                        # but six were swept
    )
    assert result.n_variants_tested == 6


def test_provenance_reports_n_variants_tested_not_just_traded():
    # The provenance footer must surface the SWEPT count (the search width), so a reviewer
    # sees the full researcher degrees-of-freedom -- not the smaller "configs that traded".
    result = OptimizeResult(
        in_sample={"ext_2.0": _Dummy(), "ext_1.5": _Dummy()},  # 2 traded
        out_of_sample={},
        winner="ext_1.5",
        n_variants_tested=5,                                    # 5 swept (analyst widened)
    )
    prov = _provenance(result)
    assert "n_variants_tested=5" in prov


def test_provenance_defaults_to_traded_count_when_swept_size_unknown():
    # Back-compat: a result built without n_variants_tested (summary-only callers) falls
    # back to the in-sample size so the footer is never silently empty.
    result = OptimizeResult(
        in_sample={"ext_2.0": _Dummy(), "ext_1.5": _Dummy()},
        out_of_sample={},
        winner="ext_1.5",
    )
    prov = _provenance(result)
    assert "n_variants_tested=2" in prov


class _Dummy:
    """A stand-in PerformanceSummary just for dict membership/len in provenance tests."""

    n_closed = 1

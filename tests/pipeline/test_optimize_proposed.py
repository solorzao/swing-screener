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
    # the proposed variant joins the grid under its NAMESPACED key (collision-proof)
    assert "proposed:min_pb_2_q" in grid
    assert grid["proposed:min_pb_2_q"].min_pullback_bars == 2
    # the deterministic sweep points are still present
    assert "ext_2.0" in grid


def test_non_queued_variant_is_not_merged():
    base = StrategyConfig()
    pv = _queued("draft_q", {"min_pullback_bars": 2}, status="draft")
    grid = build_config_grid(base, proposed=[pv])
    assert "proposed:draft_q" not in grid


def test_invalid_delta_variant_is_skipped_not_in_grid(caplog):
    # An illegal (frozen-indicator) delta must NOT poison the grid; it is skipped + logged.
    base = StrategyConfig()
    good = _queued("good_q", {"min_pullback_bars": 2})
    bad = _queued("bad_q", {"ema_fast": 10})  # frozen indicator field -> to_config raises
    with caplog.at_level(logging.WARNING):
        grid = build_config_grid(base, proposed=[good, bad])
    assert "proposed:good_q" in grid
    assert "proposed:bad_q" not in grid
    assert any("bad_q" in r.message for r in caplog.records)


def test_unknown_key_variant_is_skipped_not_in_grid(caplog):
    base = StrategyConfig()
    bad = _queued("typo_q", {"not_a_knob": 1.0})
    with caplog.at_level(logging.WARNING):
        grid = build_config_grid(base, proposed=[bad])
    assert "proposed:typo_q" not in grid
    assert any("typo_q" in r.message for r in caplog.records)


def test_noop_delta_equal_to_incumbent_is_skipped_not_in_grid(caplog):
    # Defense in depth behind reflect's drafting guard: a queued delta equal to the shipped
    # defaults would re-sweep the incumbent (ext_<gate>) arm under a second name -- a wasted
    # slot that ALSO inflates n_variants_tested for a config already in the bake-off.
    base = StrategyConfig()
    noop = _queued("noop_q", {"max_extension_atr": base.max_extension_atr})
    with caplog.at_level(logging.WARNING):
        grid = build_config_grid(base, proposed=[noop])
    assert "proposed:noop_q" not in grid
    # honest accounting: the grid did NOT grow for the no-op arm
    assert len(grid) == len(build_config_grid(base))
    assert any("noop_q" in r.message for r in caplog.records)


def test_grid_without_proposed_is_unchanged():
    base = StrategyConfig()
    assert build_config_grid(base) == build_config_grid(base, proposed=[])
    assert build_config_grid(base, proposed=None) == build_config_grid(base)


# =====================================================================================
# collision safety -- a queued variant can NEVER silently overwrite a deterministic /
# incumbent sweep point or another queued variant (honesty-hardening, Phase-6 fix)
# =====================================================================================
def test_queued_name_colliding_with_deterministic_key_does_not_overwrite_incumbent():
    # A queued variant named after a deterministic sweep point (e.g. "ext_2.0") must NOT
    # clobber that point -- the incumbent baseline propose()'s gate relies on stays intact,
    # and the proposed variant still enters the grid under its own namespaced key.
    base = StrategyConfig()  # default max_extension_atr=2.0 -> incumbent key "ext_2.0"
    incumbent_cfg = build_config_grid(base)["ext_2.0"]
    # a malicious/colliding name carrying a DIFFERENT config (would silently overwrite ext_2.0)
    pv = _queued("ext_2.0", {"min_pullback_bars": 7})
    grid = build_config_grid(base, proposed=[pv])
    # the deterministic incumbent point is PRESERVED (not replaced by the proposed delta)
    assert "ext_2.0" in grid
    assert grid["ext_2.0"] == incumbent_cfg
    assert grid["ext_2.0"].min_pullback_bars != 7
    # the proposed variant still entered the grid under its own (namespaced) key
    proposed_keys = [k for k, c in grid.items() if c.min_pullback_bars == 7]
    assert len(proposed_keys) == 1
    assert "ext_2.0" not in proposed_keys
    # honest accounting: the deterministic arms + the one proposed arm are all counted
    assert len(grid) == len(build_config_grid(base)) + 1


def test_two_queued_variants_with_same_name_dedupe_keep_first_skip_rest(caplog):
    # Two queued variants mapping to the SAME grid key: keep the first, skip the rest with a
    # warning -- a duplicate analyst name is dropped, never silently collapsed/overwritten.
    base = StrategyConfig()
    first = _queued("dup_q", {"min_pullback_bars": 2})
    second = _queued("dup_q", {"min_pullback_bars": 9})
    with caplog.at_level(logging.WARNING):
        grid = build_config_grid(base, proposed=[first, second])
    # exactly one arm for that name, and it is the FIRST one (not overwritten by the second)
    matching = [c for k, c in grid.items() if c.min_pullback_bars in (2, 9)]
    assert len(matching) == 1
    assert matching[0].min_pullback_bars == 2
    assert any("dup_q" in r.message for r in caplog.records)
    # honest accounting: only ONE distinct arm was added for the duplicate name
    assert len(grid) == len(build_config_grid(base)) + 1


def test_n_variants_tested_honest_under_collision_and_dedupe():
    # n_variants_tested must reflect the TRUE distinct swept count: a collision with a
    # deterministic key adds one arm (not zero -- no overwrite), and a duplicate queued name
    # adds one arm (not two). No undercount, no overwrite.
    base = StrategyConfig()
    n_plain = len(build_config_grid(base))
    # collides with deterministic "ext_2.0" + a duplicate pair -> 2 distinct proposed arms
    pvs = [
        _queued("ext_2.0", {"min_pullback_bars": 7}),   # collides w/ deterministic -> +1
        _queued("dup_q", {"max_pullback_bars": 6}),      # +1
        _queued("dup_q", {"max_pullback_bars": 8}),      # duplicate name -> skipped
    ]
    grid = build_config_grid(base, proposed=pvs)
    assert len(grid) == n_plain + 2


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

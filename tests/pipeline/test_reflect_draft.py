"""Tests for the OPTIONAL Opus variant-DRAFTING seam of ``pipeline.reflect`` (Task 6).

North Star #9: on a "needs a test" hunch the reflection's Opus author may DRAFT candidate
SCREEN VARIANTS -- a small NON-indicator ``StrategyConfig`` delta keyed to a hunch -- which it
QUEUES into ``edge/<pt>.proposed.json`` for the optimizer to sweep. The LLM only PROPOSES;
code owns the schema, the validation, and the human gate:

  * ``to_config`` is the HARD gate: a drafted candidate whose delta is illegal (a frozen
    indicator field) or malformed (an unknown key) is DROPPED with a warning -- never persisted,
    never swept.
  * On ANY failure (client error / empty / unparseable) -> draft NOTHING (the store is simply
    not updated), exactly like the author's deterministic fallback.
  * A surviving valid candidate is written ``status="queued"`` and then merges into the
    optimizer grid (``build_config_grid`` with ``load_proposed_for``) -- it is SWEPT, namespaced.

No network: the drafter is an injectable seam and every test passes a fake (a function that
returns canned drafts), mirroring ``author_edge_file``'s injectable client.
"""

import logging
from pathlib import Path

import pytest

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.optimize import build_config_grid
from swing_screener.pipeline.proposed import (
    QUEUED,
    ProposedVariant,
    decide_proposal,
    load_proposed_for,
    proposed_to_json,
    to_config,
)
from swing_screener.pipeline.reflect import (
    Verdict,
    draft_variants,
)


def _hunch(
    *, play_type: str = "continuation", dimension: str = "market_trend", bucket: str = "bull"
) -> Verdict:
    return Verdict(
        play_type=play_type, dimension=dimension, bucket=bucket, tier="hunch",
        n=12, expectancy_r=0.1, ci_low=-0.2, n_clusters=4, source="none",
    )


# A fake drafter returns a list of (delta, rationale, hunch_ref) tuples for the play type's
# hunches -- the structured-output the model would produce, with NO network. The seam takes
# the play type, the hunch verdicts, and the base config; the fake ignores them and replays
# canned drafts (or raises, to exercise the fail-safe path).
def _fake_drafter(drafts):
    def _fn(play_type, hunches, base):
        return list(drafts)
    return _fn


def _raising_drafter():
    def _fn(play_type, hunches, base):
        raise RuntimeError("model unavailable")
    return _fn


# =====================================================================================
# A valid drafted candidate -> a queued ProposedVariant written to edge/<pt>.proposed.json
# =====================================================================================
def test_valid_candidate_is_written_queued(tmp_path):
    base = StrategyConfig()
    drafter = _fake_drafter([
        {
            "delta": {"max_extension_atr": 1.5},
            "rationale": "A tighter freshness gate may avoid late chases in bull regimes.",
            "hunch_ref": "continuation:market_trend=bull",
        }
    ])
    written = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path,
        today="2026-06-20", drafter=drafter,
    )
    assert len(written) == 1
    # Persisted + parseable via load_proposed.
    queued = load_proposed_for("continuation", tmp_path)
    assert len(queued) == 1
    pv = queued[0]
    assert pv.status == QUEUED
    assert pv.drafted_at == "2026-06-20"
    assert pv.play_type == "continuation"
    assert pv.delta == {"max_extension_atr": 1.5}
    assert pv.provenance  # stamped (e.g. "reflection-opus")
    # Legal via the gatekeeper (it survived because to_config accepted it).
    cfg = to_config(pv, base)
    assert cfg.max_extension_atr == 1.5


def test_valid_candidate_merges_into_the_optimizer_grid(tmp_path):
    # Integration: a drafted valid candidate, once queued, is SWEPT by build_config_grid
    # (Task 5) under its namespaced key.
    base = StrategyConfig()
    drafter = _fake_drafter([
        {
            "delta": {"min_pullback_bars": 2},
            "rationale": "Require a slightly deeper pullback.",
            "hunch_ref": "continuation:market_trend=bull",
        }
    ])
    draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path,
        today="2026-06-20", drafter=drafter,
    )
    queued = load_proposed_for("continuation", tmp_path)
    grid = build_config_grid(base, proposed=queued)
    # the drafted candidate is swept (namespaced), with its delta applied
    swept = [c for k, c in grid.items() if k.startswith("proposed:")]
    assert len(swept) == 1
    assert swept[0].min_pullback_bars == 2


# =====================================================================================
# An illegal / malformed candidate -> DROPPED (not written), warned, no failure
# =====================================================================================
def test_illegal_indicator_delta_is_dropped_and_warned(tmp_path, caplog):
    base = StrategyConfig()
    drafter = _fake_drafter([
        {
            "delta": {"ema_fast": 10},  # frozen indicator field -> to_config raises
            "rationale": "Faster EMA.",
            "hunch_ref": "continuation:market_trend=bull",
        }
    ])
    with caplog.at_level(logging.WARNING):
        written = draft_variants(
            "continuation", [_hunch()], base, edge_dir=tmp_path,
            today="2026-06-20", drafter=drafter,
        )
    assert written == []
    # Nothing persisted.
    assert load_proposed_for("continuation", tmp_path) == []
    assert not (tmp_path / "continuation.proposed.json").exists()
    assert any("ema_fast" in r.message or "drop" in r.message.lower()
               for r in caplog.records)


def test_unknown_key_delta_is_dropped_and_warned(tmp_path, caplog):
    base = StrategyConfig()
    drafter = _fake_drafter([
        {
            "delta": {"not_a_knob": 1.0},
            "rationale": "Tweak.",
            "hunch_ref": "continuation:market_trend=bull",
        }
    ])
    with caplog.at_level(logging.WARNING):
        written = draft_variants(
            "continuation", [_hunch()], base, edge_dir=tmp_path,
            today="2026-06-20", drafter=drafter,
        )
    assert written == []
    assert load_proposed_for("continuation", tmp_path) == []


def test_noop_delta_equal_to_incumbent_default_is_dropped_and_warned(tmp_path, caplog):
    # A delta whose values all equal the shipped StrategyConfig defaults is a NO-OP arm:
    # sweeping it re-tests the incumbent config under a second name and burns a sweep slot
    # on a settled question (the 2026-07-02 max_extension_atr=2.0 draft did exactly this).
    base = StrategyConfig()
    drafter = _fake_drafter([
        {
            "delta": {"max_extension_atr": base.max_extension_atr},  # == the incumbent
            "rationale": "Tighten the freshness gate.",
            "hunch_ref": "continuation:volatility_tier=high",
        }
    ])
    with caplog.at_level(logging.WARNING):
        written = draft_variants(
            "continuation", [_hunch()], base, edge_dir=tmp_path,
            today="2026-07-04", drafter=drafter,
        )
    assert written == []
    # Never persisted -- the optimizer can never sweep it.
    assert load_proposed_for("continuation", tmp_path) == []
    assert not (tmp_path / "continuation.proposed.json").exists()
    assert any("no-op" in r.message.lower() for r in caplog.records)


def test_delta_differing_from_incumbent_on_any_key_is_kept(tmp_path):
    # The no-op gate drops only a delta IDENTICAL to the incumbent config: a multi-key
    # delta that restates one default but changes another still produces a distinct arm.
    base = StrategyConfig()
    drafter = _fake_drafter([
        {
            "delta": {"max_extension_atr": base.max_extension_atr, "min_pullback_bars": 3},
            "rationale": "Deeper pullback at the incumbent gate.",
            "hunch_ref": "continuation:market_trend=bull",
        }
    ])
    written = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path,
        today="2026-07-04", drafter=drafter,
    )
    assert len(written) == 1
    assert load_proposed_for("continuation", tmp_path)[0].delta == {
        "max_extension_atr": base.max_extension_atr, "min_pullback_bars": 3,
    }


def test_one_illegal_among_valid_drops_only_the_illegal(tmp_path):
    base = StrategyConfig()
    drafter = _fake_drafter([
        {"delta": {"min_pullback_bars": 2}, "rationale": "ok", "hunch_ref": "h1"},
        {"delta": {"atr_period": 20}, "rationale": "bad", "hunch_ref": "h2"},  # frozen
    ])
    written = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path,
        today="2026-06-20", drafter=drafter,
    )
    assert len(written) == 1
    queued = load_proposed_for("continuation", tmp_path)
    assert len(queued) == 1
    assert queued[0].delta == {"min_pullback_bars": 2}


# =====================================================================================
# fail-safe: a drafter that raises / returns empty -> NO draft, store unchanged
# =====================================================================================
def test_drafter_that_raises_writes_nothing(tmp_path):
    base = StrategyConfig()
    written = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path,
        today="2026-06-20", drafter=_raising_drafter(),
    )
    assert written == []
    assert not (tmp_path / "continuation.proposed.json").exists()


def test_drafter_returning_empty_writes_nothing(tmp_path):
    base = StrategyConfig()
    written = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path,
        today="2026-06-20", drafter=_fake_drafter([]),
    )
    assert written == []
    assert not (tmp_path / "continuation.proposed.json").exists()


def test_existing_queued_store_untouched_on_failure(tmp_path):
    # A prior valid queued draft must survive a later failed/empty draft run (no clobber).
    base = StrategyConfig()
    draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-06-20",
        drafter=_fake_drafter([
            {"delta": {"max_extension_atr": 1.5}, "rationale": "r", "hunch_ref": "h"}
        ]),
    )
    before = (tmp_path / "continuation.proposed.json").read_text(encoding="utf-8")
    # A later run that fails must NOT touch the store.
    draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-06-21",
        drafter=_raising_drafter(),
    )
    after = (tmp_path / "continuation.proposed.json").read_text(encoding="utf-8")
    assert after == before


def test_no_hunches_writes_nothing(tmp_path):
    # A play type with no hunch verdicts -> the drafter is never asked -> no store.
    base = StrategyConfig()
    sentinel = {"called": False}

    def _drafter(play_type, hunches, base):
        sentinel["called"] = True
        return []

    written = draft_variants(
        "continuation", [], base, edge_dir=tmp_path, today="2026-06-20", drafter=_drafter,
    )
    assert written == []
    assert sentinel["called"] is False
    assert not (tmp_path / "continuation.proposed.json").exists()


def test_rewrites_the_queued_set_each_run(tmp_path):
    # QUEUED drafts are machine-owned: a fresh successful draft REPLACES the prior queued set
    # (documented behavior), so a stale candidate doesn't accumulate forever.
    base = StrategyConfig()
    draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-06-20",
        drafter=_fake_drafter([
            {"delta": {"max_extension_atr": 1.5}, "rationale": "r1", "hunch_ref": "h1"}
        ]),
    )
    draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-06-21",
        drafter=_fake_drafter([
            {"delta": {"min_pullback_bars": 3}, "rationale": "r2", "hunch_ref": "h2"}
        ]),
    )
    queued = load_proposed_for("continuation", tmp_path)
    assert len(queued) == 1
    assert queued[0].delta == {"min_pullback_bars": 3}


@pytest.mark.parametrize("bad_draft", [
    {"rationale": "no delta", "hunch_ref": "h"},          # missing delta
    {"delta": "not-a-dict", "rationale": "r", "hunch_ref": "h"},  # delta wrong type
    {"delta": {"min_pullback_bars": 2}},                  # missing rationale/hunch_ref
])
def test_structurally_malformed_draft_is_dropped(tmp_path, bad_draft):
    # A draft missing required fields or with a non-dict delta is dropped, not crashed on.
    base = StrategyConfig()
    written = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-06-20",
        drafter=_fake_drafter([bad_draft]),
    )
    assert written == []
    assert load_proposed_for("continuation", tmp_path) == []


# =====================================================================================
# MERGE (Phase 3): a redraft replaces only the QUEUED set -- non-queued rows survive it,
# and a fresh draft colliding with a preserved row's name is BLOCKED (preserved rows win).
# =====================================================================================
def _seeded_row(
    *,
    name: str,
    status: str,
    delta: dict[str, float | int | str | bool] | None = None,
) -> ProposedVariant:
    return ProposedVariant(
        name=name, play_type="continuation",
        delta={"min_pullback_bars": 2} if delta is None else delta,
        rationale="seeded", hunch_ref="continuation:seeded", status=status,
        drafted_at="2026-06-01", provenance="reflection-opus",
    )


def test_redraft_preserves_non_queued_rows_beside_the_fresh_queued_set(tmp_path: Path) -> None:
    # Phase 3 cockpit decisions (approved/withdrawn) are an audit record living in the file
    # until git captures it. A Sunday reflection redraft must NOT clobber them: the new file
    # is (non-queued rows, original order, untouched) + (fresh queued); ONLY the stale
    # QUEUED rows are superseded.
    base = StrategyConfig()
    withdrawn = _seeded_row(name="continuation_old_idea_0_q", status="withdrawn")
    approved = _seeded_row(
        name="continuation_kept_idea_1_q", status="approved", delta={"min_pullback_bars": 3},
    )
    draft = _seeded_row(
        name="continuation_half_baked_2_q", status="draft", delta={"max_extension_atr": 1.8},
    )
    stale_queued = _seeded_row(
        name="continuation_stale_3_q", status=QUEUED, delta={"max_extension_atr": 1.2},
    )
    (tmp_path / "continuation.proposed.json").write_text(
        proposed_to_json([withdrawn, approved, draft, stale_queued]), encoding="utf-8"
    )
    written = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-07-12",
        drafter=_fake_drafter([
            {"delta": {"max_extension_atr": 1.5}, "rationale": "fresh", "hunch_ref": "h_new"}
        ]),
    )
    assert len(written) == 1
    stored = load_proposed_for("continuation", tmp_path)
    # Non-queued rows first, in their original order, preserved verbatim (the round-trip is
    # lossless, so dataclass equality IS byte-level fidelity of every field).
    assert stored[:3] == [withdrawn, approved, draft]
    # Then the fresh queued set; the stale queued row is superseded (gone).
    assert stored[3:] == written
    assert all(pv.name != stale_queued.name for pv in stored)
    # The merge write is atomic (tmp + os.replace): the .tmp sibling never outlives the call.
    assert [p.name for p in tmp_path.iterdir()] == ["continuation.proposed.json"]


def test_collision_with_a_decided_row_blocks_the_fresh_draft(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    # A REAL collision via _draft_name determinism: a durable hunch re-drafts the same idea
    # at the same index -> the SAME deterministic name. Once that row is DECIDED (withdrawn
    # here), the redraft must not resurrect it: preserved rows win, the fresh draft is
    # dropped with a log, and a withdrawn idea is not silently re-queued.
    base = StrategyConfig()
    drafts = [{
        "delta": {"max_extension_atr": 1.5},
        "rationale": "A tighter freshness gate.",
        "hunch_ref": "continuation:market_trend=bull",
    }]
    first = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-07-05",
        drafter=_fake_drafter(drafts),
    )
    assert len(first) == 1
    withdrawn = decide_proposal(
        tmp_path, "continuation", first[0].name,
        decision="withdrawn", reason="not worth a sweep slot", today="2026-07-08",
    )
    # The SAME drafter output a week later -> the same play_type + hunch_ref + index.
    with caplog.at_level(logging.INFO):
        second = draft_variants(
            "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-07-12",
            drafter=_fake_drafter(drafts),
        )
    assert second == []
    # The decision survives; the idea is NOT re-queued.
    assert load_proposed_for("continuation", tmp_path) == [withdrawn]
    assert any("preserved rows win" in r.message for r in caplog.records)


def test_unreadable_prior_store_queues_nothing_and_is_left_untouched(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    # The merge READS the prior store, so a corrupt/hand-mangled file is a new failure mode.
    # It may hold the only copy of a human decision: never clobber it, never crash the
    # reflection -- warn, queue nothing, leave the bytes exactly as found (fail-safe).
    base = StrategyConfig()
    (tmp_path / "continuation.proposed.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        written = draft_variants(
            "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-07-12",
            drafter=_fake_drafter([
                {"delta": {"max_extension_atr": 1.5}, "rationale": "r", "hunch_ref": "h"}
            ]),
        )
    assert written == []
    assert (tmp_path / "continuation.proposed.json").read_text(encoding="utf-8") == "{not json"
    assert any("store untouched" in r.message for r in caplog.records)


def test_collision_blocks_only_the_colliding_fresh_draft(tmp_path: Path) -> None:
    # Two fresh drafts: the durable hunch re-drafts at index 0 (same name as the withdrawn
    # row -> blocked); a genuinely new idea at index 1 gets a new name -> queued beside it.
    base = StrategyConfig()
    durable = {
        "delta": {"max_extension_atr": 1.5},
        "rationale": "A tighter freshness gate.",
        "hunch_ref": "continuation:market_trend=bull",
    }
    first = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-07-05",
        drafter=_fake_drafter([durable]),
    )
    withdrawn = decide_proposal(
        tmp_path, "continuation", first[0].name,
        decision="withdrawn", reason="overlaps the ext sweep", today="2026-07-08",
    )
    new_idea = {
        "delta": {"min_pullback_bars": 3},
        "rationale": "Deeper pullback.",
        "hunch_ref": "continuation:volatility_tier=high",
    }
    second = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-07-12",
        drafter=_fake_drafter([durable, new_idea]),
    )
    assert [pv.delta for pv in second] == [{"min_pullback_bars": 3}]
    stored = load_proposed_for("continuation", tmp_path)
    # Decided row first, then the surviving fresh queued draft.
    assert stored == [withdrawn, *second]
    assert [pv.status for pv in stored] == ["withdrawn", QUEUED]


def test_all_colliding_redraft_still_supersedes_stale_queued_rows(tmp_path: Path) -> None:
    # The one edge where "every candidate dropped" does NOT mean "store untouched":
    # collision drops happen AFTER a successful draft (they are not validation failures),
    # so the merge write proceeds -- the stale queued set is superseded even though nothing
    # new queues, and the decided row survives as the only content.
    base = StrategyConfig()
    durable = {
        "delta": {"max_extension_atr": 1.5},
        "rationale": "A tighter freshness gate.",
        "hunch_ref": "continuation:market_trend=bull",
    }
    new_idea = {
        "delta": {"min_pullback_bars": 3},
        "rationale": "Deeper pullback.",
        "hunch_ref": "continuation:volatility_tier=high",
    }
    first = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-07-05",
        drafter=_fake_drafter([durable]),
    )
    withdrawn = decide_proposal(
        tmp_path, "continuation", first[0].name,
        decision="withdrawn", reason="overlaps the ext sweep", today="2026-07-08",
    )
    # A second run queues new_idea (index 1) beside the withdrawn row.
    second = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-07-12",
        drafter=_fake_drafter([durable, new_idea]),
    )
    assert len(second) == 1
    # A third run drafts ONLY the durable idea -> its one candidate collides -> fresh == [].
    third = draft_variants(
        "continuation", [_hunch()], base, edge_dir=tmp_path, today="2026-07-19",
        drafter=_fake_drafter([durable]),
    )
    assert third == []
    # The write still happened: the stale queued row from the second run is gone; the
    # withdrawn decision is preserved.
    assert load_proposed_for("continuation", tmp_path) == [withdrawn]

"""Tests for ``decide_proposal`` -- the first TOOL-written proposal transition (Phase 3).

The 2026-07 convention was a HAND-EDITED withdrawal: a human appended ``WITHDRAWN <date>:
<reason>`` to the rationale and flipped ``status`` in ``edge/<pt>.proposed.json``. Phase 3's
cockpit writes those decisions through code instead, and the data layer owns the transition:

  * ``decide_proposal(edge_dir, play_type, name, *, decision, reason, today)`` loads the
    play type's store, transitions exactly ONE row, and rewrites the file (order preserved,
    every other row untouched). It returns the updated row.
  * The state machine: approve requires ``status == "queued"``; withdraw requires ``status``
    in ``("queued", "approved")``. Anything else is a ``ValueError`` naming the current
    status; an unknown name is a ``KeyError``.
  * The audit trail matches the hand-edit convention EXACTLY: the rationale gains
    ``f" {decision.upper()} {today}: {reason}"`` -- e.g. `` WITHDRAWN 2026-07-03: ...``.
  * ``"approved"`` is NEW to the data. The optimizer sweeps ONLY ``status == "queued"``
    (``build_config_grid``), so an approved row must NOT re-enter the sweep.

Pure data layer -- no LLM, no network.
"""

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
)


def _pv(
    *,
    name: str = "continuation_h_0_q",
    status: str = QUEUED,
    delta: dict[str, float | int | str | bool] | None = None,
    rationale: str = "A tighter freshness gate may avoid late chases.",
) -> ProposedVariant:
    return ProposedVariant(
        name=name, play_type="continuation",
        delta={"max_extension_atr": 1.5} if delta is None else delta,
        rationale=rationale, hunch_ref="continuation:market_trend=bull",
        status=status, drafted_at="2026-07-05", provenance="reflection-opus",
    )


def _seed(edge_dir: Path, items: list[ProposedVariant], play_type: str = "continuation") -> None:
    (edge_dir / f"{play_type}.proposed.json").write_text(
        proposed_to_json(items), encoding="utf-8"
    )


# =====================================================================================
# happy paths -- approve, withdraw, withdraw-after-approve
# =====================================================================================
def test_approve_a_queued_row(tmp_path: Path) -> None:
    row = _pv(rationale="Worth a controlled test.")
    _seed(tmp_path, [row])
    updated = decide_proposal(
        tmp_path, "continuation", row.name,
        decision="approved", reason="cleared the walk-forward bound", today="2026-07-11",
    )
    assert updated.status == "approved"
    # The audit append matches the hand-edit convention EXACTLY.
    assert updated.rationale == (
        "Worth a controlled test. APPROVED 2026-07-11: cleared the walk-forward bound"
    )
    # Every other field is untouched (a dataclasses.replace of the original).
    assert updated.name == row.name
    assert updated.delta == row.delta
    assert updated.drafted_at == row.drafted_at
    # Persisted: the store round-trips to the updated row.
    assert load_proposed_for("continuation", tmp_path) == [updated]


def test_withdraw_a_queued_row(tmp_path: Path) -> None:
    row = _pv(rationale="Maybe.")
    _seed(tmp_path, [row])
    updated = decide_proposal(
        tmp_path, "continuation", row.name,
        decision="withdrawn", reason="overlaps the ext sweep", today="2026-07-03",
    )
    assert updated.status == "withdrawn"
    assert updated.rationale == "Maybe. WITHDRAWN 2026-07-03: overlaps the ext sweep"
    assert load_proposed_for("continuation", tmp_path) == [updated]


def test_withdraw_after_approve_keeps_the_full_audit_trail(tmp_path: Path) -> None:
    # approved -> withdrawn is legal (an approval can be walked back); the rationale then
    # carries BOTH appends, in order -- the file is the audit record until git captures it.
    row = _pv(rationale="Test it.")
    _seed(tmp_path, [row])
    decide_proposal(
        tmp_path, "continuation", row.name,
        decision="approved", reason="looked strong", today="2026-07-06",
    )
    updated = decide_proposal(
        tmp_path, "continuation", row.name,
        decision="withdrawn", reason="regressed on the pinned corpus", today="2026-07-11",
    )
    assert updated.status == "withdrawn"
    assert updated.rationale == (
        "Test it. APPROVED 2026-07-06: looked strong"
        " WITHDRAWN 2026-07-11: regressed on the pinned corpus"
    )
    assert load_proposed_for("continuation", tmp_path) == [updated]


def test_decide_rewrites_only_the_one_row_and_preserves_order(tmp_path: Path) -> None:
    first = _pv(name="continuation_a_0_q", delta={"max_extension_atr": 1.5})
    second = _pv(name="continuation_b_1_q", delta={"min_pullback_bars": 2})
    third = _pv(name="continuation_c_2_q", delta={"min_pullback_bars": 3})
    _seed(tmp_path, [first, second, third])
    updated = decide_proposal(
        tmp_path, "continuation", second.name,
        decision="approved", reason="best arm", today="2026-07-11",
    )
    assert load_proposed_for("continuation", tmp_path) == [first, updated, third]


# =====================================================================================
# error paths -- unknown name (KeyError), wrong state (ValueError naming the status)
# =====================================================================================
def test_unknown_name_raises_keyerror(tmp_path: Path) -> None:
    _seed(tmp_path, [_pv()])
    with pytest.raises(KeyError, match="continuation_nope_9_q"):
        decide_proposal(
            tmp_path, "continuation", "continuation_nope_9_q",
            decision="approved", reason="r", today="2026-07-11",
        )


def test_missing_store_raises_keyerror(tmp_path: Path) -> None:
    # No edge/<pt>.proposed.json at all -> the name cannot exist -> KeyError, not a crash.
    with pytest.raises(KeyError, match="continuation_h_0_q"):
        decide_proposal(
            tmp_path, "continuation", "continuation_h_0_q",
            decision="withdrawn", reason="r", today="2026-07-11",
        )


def test_unknown_decision_is_rejected_at_runtime(tmp_path: Path) -> None:
    # Literal["approved", "withdrawn"] is STATIC-only: a runtime caller passing "rejected"
    # would otherwise write a status the state machine doesn't know -- and the draft merge
    # preserves every non-queued status forever. Guard it like to_config guards deltas.
    row = _pv()
    _seed(tmp_path, [row])
    with pytest.raises(ValueError, match="rejected"):
        decide_proposal(
            tmp_path, "continuation", row.name,
            decision="rejected",  # type: ignore[arg-type]
            reason="r", today="2026-07-11",
        )
    assert load_proposed_for("continuation", tmp_path) == [row]


@pytest.mark.parametrize("status", ["withdrawn", "approved", "draft"])
def test_approve_requires_a_queued_row(tmp_path: Path, status: str) -> None:
    row = _pv(status=status)
    _seed(tmp_path, [row])
    with pytest.raises(ValueError, match=status):
        decide_proposal(
            tmp_path, "continuation", row.name,
            decision="approved", reason="r", today="2026-07-11",
        )
    # The store is untouched on a refused transition.
    assert load_proposed_for("continuation", tmp_path) == [row]


@pytest.mark.parametrize("status", ["withdrawn", "draft"])
def test_withdraw_requires_queued_or_approved(tmp_path: Path, status: str) -> None:
    row = _pv(status=status)
    _seed(tmp_path, [row])
    with pytest.raises(ValueError, match=status):
        decide_proposal(
            tmp_path, "continuation", row.name,
            decision="withdrawn", reason="r", today="2026-07-11",
        )
    assert load_proposed_for("continuation", tmp_path) == [row]


def test_decision_write_leaves_no_tmp_sibling(tmp_path: Path) -> None:
    # The rewrite is atomic (serialize to a .tmp sibling, then os.replace): this file holds
    # the only uncommitted copy of human decisions, and a torn write is the one corruption
    # the draft merge's fail-safe read guard cannot undo. Observable: the .tmp sibling never
    # outlives the call.
    row = _pv()
    _seed(tmp_path, [row])
    decide_proposal(
        tmp_path, "continuation", row.name,
        decision="approved", reason="r", today="2026-07-11",
    )
    assert [p.name for p in tmp_path.iterdir()] == ["continuation.proposed.json"]


# =====================================================================================
# the sweep boundary -- an approved row is NOT re-swept by the optimizer
# =====================================================================================
def test_approved_row_is_not_swept_by_build_config_grid(tmp_path: Path) -> None:
    # "approved" is new to the data; build_config_grid merges ONLY status == "queued", so
    # approving a row retires it from the sweep (its promotion path is propose(), not
    # another walk-forward slot) while a still-queued sibling keeps sweeping.
    approved = _pv(name="continuation_a_0_q", delta={"max_extension_atr": 1.5})
    queued = _pv(name="continuation_b_1_q", delta={"min_pullback_bars": 2})
    _seed(tmp_path, [approved, queued])
    decide_proposal(
        tmp_path, "continuation", approved.name,
        decision="approved", reason="promote it", today="2026-07-11",
    )
    grid = build_config_grid(StrategyConfig(), proposed=load_proposed_for("continuation", tmp_path))
    assert f"proposed:{approved.name}" not in grid
    assert f"proposed:{queued.name}" in grid

"""Tests for the validated ProposedVariant store + the to_config gatekeeper (Phase-6 Task 5).

The analyst (Task 6) drafts candidate SCREEN VARIANTS on a "needs a test" hunch and QUEUES
them into ``edge/<pt>.proposed.json``. This is the validated data layer they land in:

  * ``ProposedVariant`` -- a frozen, JSON-native record of one queued variant (a StrategyConfig
    delta + provenance); ``proposed_to_json`` / ``load_proposed`` round-trip it losslessly
    (mirroring ``reflect.verdicts_to_json`` / ``load_verdicts``).
  * ``to_config`` -- THE GATEKEEPER: turns a delta into a StrategyConfig and validates it, so a
    malformed/illegal delta NEVER reaches the optimizer grid. A LEGAL delta (a detection knob)
    yields the replaced config; an ILLEGAL delta (a frozen indicator field) or an UNKNOWN key
    raises a clear error.
  * ``load_proposed_for`` -- reads one play type's queued variants off disk (missing -> []).

NO LLM, NO network -- pure data + validation.
"""

import dataclasses

import pytest

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.proposed import (
    ProposedVariant,
    load_proposed,
    load_proposed_for,
    proposed_to_json,
    to_config,
)


def _pv(
    *,
    name: str = "extguard_tight_q",
    play_type: str = "continuation",
    delta: dict | None = None,
    rationale: str = "A tighter freshness gate may avoid late chases.",
    hunch_ref: str = "continuation:market_trend=bull",
    status: str = "queued",
    drafted_at: str = "2026-06-20",
    provenance: str = "analyst@claude-opus-4-8",
) -> ProposedVariant:
    return ProposedVariant(
        name=name, play_type=play_type,
        delta={"max_extension_atr": 1.5} if delta is None else delta,
        rationale=rationale, hunch_ref=hunch_ref, status=status,
        drafted_at=drafted_at, provenance=provenance,
    )


# =====================================================================================
# to_config -- the gatekeeper: legal -> replaced config; illegal/unknown -> raises
# =====================================================================================
def test_to_config_applies_a_legal_delta():
    base = StrategyConfig()
    pv = _pv(delta={"max_extension_atr": 1.5})
    cfg = to_config(pv, base)
    assert cfg.max_extension_atr == 1.5
    # everything else is unchanged (it's a dataclasses.replace of the base)
    assert cfg.min_pullback_bars == base.min_pullback_bars
    assert cfg.ema_fast == base.ema_fast


def test_to_config_applies_a_legal_multi_field_detection_delta():
    base = StrategyConfig()
    pv = _pv(delta={"min_pullback_bars": 2, "oversold_rsi_max": 30.0})
    cfg = to_config(pv, base)
    assert cfg.min_pullback_bars == 2
    assert cfg.oversold_rsi_max == 30.0


def test_to_config_rejects_an_illegal_indicator_delta():
    # ema_fast is a frozen indicator field: changing it would score against the wrong frame.
    base = StrategyConfig()
    pv = _pv(delta={"ema_fast": 10})
    with pytest.raises(ValueError, match="indicator field"):
        to_config(pv, base)


def test_to_config_rejects_an_illegal_indicator_delta_among_legal_ones():
    # A mixed delta with one frozen indicator field must still be rejected wholesale.
    base = StrategyConfig()
    pv = _pv(delta={"max_extension_atr": 1.5, "atr_period": 20})
    with pytest.raises(ValueError, match="indicator field"):
        to_config(pv, base)


def test_to_config_rejects_an_unknown_key_with_a_clear_error():
    base = StrategyConfig()
    pv = _pv(delta={"not_a_real_knob": 1.0})
    with pytest.raises(ValueError, match="unknown"):
        to_config(pv, base)


def test_to_config_error_names_the_variant_and_the_unknown_key():
    base = StrategyConfig()
    pv = _pv(name="bad_q", delta={"totally_made_up": 3})
    with pytest.raises(ValueError) as ei:
        to_config(pv, base)
    msg = str(ei.value)
    assert "bad_q" in msg and "totally_made_up" in msg


# =====================================================================================
# round-trip -- proposed_to_json / load_proposed preserve every field
# =====================================================================================
def test_proposed_json_round_trip_preserves_all_fields():
    items = [
        _pv(),
        _pv(
            name="rev_deep_q", play_type="reversal",
            delta={"reversal_pullback_deep": 0.7, "reversal_min_bearish_bars": 4},
            status="queued",
        ),
    ]
    assert load_proposed(proposed_to_json(items)) == items


def test_proposed_json_empty_round_trips():
    assert load_proposed(proposed_to_json([])) == []


def test_proposed_variant_is_frozen():
    pv = _pv()
    with pytest.raises(dataclasses.FrozenInstanceError):
        pv.name = "mutated"  # type: ignore[misc]


# =====================================================================================
# load_proposed_for -- reads edge/<pt>.proposed.json; missing -> []
# =====================================================================================
def test_load_proposed_for_missing_file_returns_empty(tmp_path):
    assert load_proposed_for("continuation", tmp_path) == []


def test_load_proposed_for_reads_the_play_types_file(tmp_path):
    items = [_pv(play_type="continuation"), _pv(name="b_q", play_type="continuation")]
    (tmp_path / "continuation.proposed.json").write_text(
        proposed_to_json(items), encoding="utf-8"
    )
    assert load_proposed_for("continuation", tmp_path) == items
    # a different play type's file is untouched / empty
    assert load_proposed_for("reversal", tmp_path) == []

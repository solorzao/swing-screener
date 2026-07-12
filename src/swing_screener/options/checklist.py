"""The 12-point A+ checklist that grades a setup at decision time.

Four blocks -- bias, structure, trigger, risk -- each a small group of
must-be-true confirmations. The keys match the ``OptionSetup.chk_*`` columns
exactly so a graded setup round-trips to the journal without a mapping layer.

Grading rule (from docs/modules/gex-lab.md): every box checked is an A+; the
one sanctioned concession is a missing confirmation candle, which caps the
setup at B ("No confirmation candle = B grade at best"); any other unchecked
box is a no_trade.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChecklistItem:
    key: str
    label: str
    block: str  # bias | structure | trigger | risk


CHECKLIST_ITEMS: tuple[ChecklistItem, ...] = (
    # Bias: is the higher-timeframe direction unambiguous?
    ChecklistItem("chk_daily_bias_clear", "Daily bias is clear, not chop", "bias"),
    ChecklistItem("chk_daily_stack_ordered", "Daily EMA stack is cleanly ordered", "bias"),
    ChecklistItem("chk_m5_agrees", "5-minute trend agrees with the daily", "bias"),
    # Structure: is price at a GEX level that matters?
    ChecklistItem("chk_gex_levels_marked", "GEX walls and flip are marked", "structure"),
    ChecklistItem("chk_price_at_pivot", "Price is at the pivot level, not mid-range", "structure"),
    ChecklistItem("chk_regime_match", "Gamma regime matches the play", "structure"),
    # Trigger: is the entry pattern actually there?
    ChecklistItem("chk_pattern_clean", "Entry pattern is clean, not forced", "trigger"),
    ChecklistItem("chk_volume_confirming", "Volume confirms the move", "trigger"),
    # Risk: is the trade sized and structured to survive?
    ChecklistItem("chk_risk_sized", "Position is risk-sized to the plan", "risk"),
    ChecklistItem("chk_stop_structural", "Stop sits behind structure", "risk"),
    ChecklistItem("chk_rr_at_least_2", "Reward-to-risk is at least 2R", "risk"),
    ChecklistItem("chk_confirmation_candle", "Confirmation candle has printed", "risk"),
)

_KEYS = frozenset(item.key for item in CHECKLIST_ITEMS)


def grade(items: dict[str, bool]) -> str:
    """Grade a filled checklist: ``"A+"`` | ``"B"`` | ``"no_trade"``.

    Every checklist key must be present and every provided key must be known;
    a mismatch raises ``KeyError`` rather than silently mis-grading.
    """
    provided = set(items)
    unknown = provided - _KEYS
    if unknown:
        raise KeyError(f"unknown checklist keys: {sorted(unknown)}")
    missing = _KEYS - provided
    if missing:
        raise KeyError(f"missing checklist keys: {sorted(missing)}")

    unchecked = {key for key, ok in items.items() if not ok}
    if not unchecked:
        return "A+"
    if unchecked == {"chk_confirmation_candle"}:
        return "B"
    return "no_trade"

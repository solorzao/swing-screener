from swing_screener.options.checklist import CHECKLIST_ITEMS, grade


def test_twelve_items_in_four_blocks() -> None:
    assert len(CHECKLIST_ITEMS) == 12
    assert {i.block for i in CHECKLIST_ITEMS} == {"bias", "structure", "trigger", "risk"}
    assert all(i.key.startswith("chk_") for i in CHECKLIST_ITEMS)


def test_all_true_is_a_plus() -> None:
    assert grade({i.key: True for i in CHECKLIST_ITEMS}) == "A+"


def test_missing_only_confirmation_candle_is_b() -> None:
    items = {i.key: True for i in CHECKLIST_ITEMS}
    items["chk_confirmation_candle"] = False
    assert grade(items) == "B"


def test_any_other_miss_is_no_trade() -> None:
    items = {i.key: True for i in CHECKLIST_ITEMS}
    items["chk_regime_match"] = False
    assert grade(items) == "no_trade"


def test_unknown_key_raises() -> None:
    import pytest
    with pytest.raises(KeyError):
        grade({"chk_bogus": True})

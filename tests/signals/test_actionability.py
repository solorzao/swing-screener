from swing_screener.signals.actionability import classify


def _z(price):
    # entry zone [98, 100], stop 96 -> risk (ceiling-stop) = 4
    return classify(entry_floor=98.0, entry_ceiling=100.0, stop=96.0, price=price)


def test_price_in_zone_is_actionable():
    a = _z(99.0)
    assert a.status == "actionable"
    assert a.dist_r is not None and a.dist_r < 0  # below the ceiling -> room to enter


def test_price_below_zone_above_stop_is_actionable():
    a = _z(97.0)
    assert a.status == "actionable"


def test_price_just_above_ceiling_within_buffer_is_actionable():
    # 100.5 is 0.5 over the ceiling -> 0.125R, inside the default 0.25R buffer
    a = _z(100.5)
    assert a.status == "actionable"
    assert a.dist_r is not None and 0 < a.dist_r <= 0.25


def test_price_well_above_ceiling_is_extended():
    a = _z(102.0)  # 2 over a 4-risk zone -> 0.5R past entry
    assert a.status == "extended"
    assert a.dist_r == 0.5


def test_price_at_or_below_stop_is_broken():
    assert _z(96.0).status == "broken"
    assert _z(95.0).status == "broken"


def test_missing_price_is_unknown():
    a = _z(None)
    assert a.status == "unknown"
    assert a.dist_r is None


def test_degenerate_zone_is_unknown():
    # ceiling <= stop -> non-positive risk, cannot classify
    a = classify(entry_floor=100.0, entry_ceiling=100.0, stop=100.0, price=101.0)
    assert a.status == "unknown"


def test_custom_buffer_tightens_actionable_band():
    # with no buffer, anything above the ceiling is immediately extended
    a = classify(entry_floor=98.0, entry_ceiling=100.0, stop=96.0, price=100.5,
                 buffer_r=0.0)
    assert a.status == "extended"

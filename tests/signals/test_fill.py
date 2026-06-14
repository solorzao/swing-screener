from swing_screener.signals.entry_zone import EntryZone
from swing_screener.signals.fill import resolve_fill

ZONE = EntryZone(floor=96.0, ceiling=101.0, stop=94.0, target=110.0, risk=4.0, reference=98.5)


def test_filled_at_worst_case_in_zone():
    # bar trades through the whole zone -> worst-case long fill is the ceiling
    res = resolve_fill(ZONE, bar_high=105.0, bar_low=97.0)
    assert res.status == "filled"
    assert res.price == 101.0  # min(bar_high, ceiling)


def test_filled_partial_overlap_uses_bar_high():
    res = resolve_fill(ZONE, bar_high=99.0, bar_low=95.0)
    assert res.status == "filled"
    assert res.price == 99.0


def test_missed_when_gap_above_ceiling():
    res = resolve_fill(ZONE, bar_high=120.0, bar_low=102.0)
    assert res.status == "missed"


def test_invalidated_when_gap_below_stop():
    res = resolve_fill(ZONE, bar_high=93.0, bar_low=90.0)
    assert res.status == "invalidated"


def test_invalidated_when_bar_entirely_below_floor_without_stop_breach():
    # bar sits below the zone floor but never tags the stop -> still no entry
    res = resolve_fill(ZONE, bar_high=95.5, bar_low=94.5)
    assert res.status == "invalidated"
    assert res.price is None

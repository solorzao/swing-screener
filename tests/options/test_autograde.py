from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from swing_screener.options.autograde import (
    HINT_KEYS,
    MACHINE_KEYS,
    AutoGrade,
    ItemVerdict,
    autograde,
)
from swing_screener.options.checklist import CHECKLIST_ITEMS
from swing_screener.options.config import GexConfig
from tests.conftest import make_bars

# ---- synthetic frames (no network, bias.py/gex.py test style) --------------

_NOW = datetime(2026, 7, 13, 18, 16)  # after the last synthetic 5m bar completes


def _daily(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"close": [float(c) for c in closes]}, index=idx)


def _uptrend_daily(n: int = 150) -> pd.DataFrame:
    return _daily([100 + 0.5 * i for i in range(n)])


def _downtrend_daily(n: int = 150) -> pd.DataFrame:
    return _daily([200 - 0.5 * i for i in range(n)])


def _chop_daily(n: int = 150) -> pd.DataFrame:
    return _daily([100 + (1 if i % 2 else -1) for i in range(n)])


def _five_min(closes: list[float], vols: list[float], *, greens: list[bool],
              start: str = "2026-07-13 09:30") -> pd.DataFrame:
    rows = []
    for c, v, green in zip(closes, vols, greens, strict=True):
        o = c - 0.2 if green else c + 0.2  # body sign, closes drive the stack
        rows.append(dict(open=o, high=max(o, c) + 0.1, low=min(o, c) - 0.1,
                         close=c, volume=v))
    return make_bars(rows, start=start, freq="5min")


def _uptrend_5m(n: int = 105, *, last_vol: float = 3_000_000.0,
                base_vol: float = 1_000_000.0, last_green: bool = True) -> pd.DataFrame:
    closes = [100 + 0.5 * i for i in range(n)]
    vols = [base_vol] * n
    vols[-1] = last_vol
    greens = [True] * n
    greens[-1] = last_green
    return _five_min(closes, vols, greens=greens)


def _downtrend_5m(n: int = 105) -> pd.DataFrame:
    closes = [150 - 0.5 * i for i in range(n)]
    return _five_min(closes, [1_000_000.0] * n, greens=[False] * n)


def _snap(**kw: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        ts=datetime(2026, 7, 13, 10, 0), spot=152.0, call_wall=155.0,
        put_wall=148.0, gamma_flip=151.0, regime="negative",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _run(*, direction: str = "long", play_type: str = "breakout",
         entry: float | None = 100.0, stop: float | None = 99.0,
         target: float | None = 103.0, pivot_level: float | None = None,
         daily_bars: pd.DataFrame | None = None, bars_5m: pd.DataFrame | None = None,
         snapshot: object | None = None, now: datetime = _NOW) -> AutoGrade:
    return autograde(
        "SPY", direction, play_type, entry, stop, target, pivot_level,
        cfg=GexConfig(), daily_bars=daily_bars, bars_5m=bars_5m,
        snapshot=snapshot, now=lambda: now,
    )


def _by_key(g: AutoGrade) -> dict[str, ItemVerdict]:
    return {v.key: v for v in g.items}


# ---- shape / key partition -------------------------------------------------

def test_items_cover_exactly_the_eight_machine_keys_in_order() -> None:
    g = _run()
    assert [v.key for v in g.items] == list(MACHINE_KEYS)
    assert len(g.items) == 8
    assert len(g.hints) == 2


def test_machine_and_hint_keys_partition_the_checklist() -> None:
    canon = {i.key for i in CHECKLIST_ITEMS}
    covered = set(MACHINE_KEYS) | set(HINT_KEYS) | {"chk_pattern_clean", "chk_risk_sized"}
    assert covered == canon
    assert set(MACHINE_KEYS).isdisjoint(HINT_KEYS)


def test_new_config_knobs_have_documented_defaults() -> None:
    cfg = GexConfig()
    assert cfg.vol_confirm_mult == 1.5
    assert cfg.vol_lookback == 20
    assert cfg.rr_min == 2.0
    assert cfg.pivot_tolerance_pct == 0.25
    assert cfg.swing_lookback == 12


# ---- item 1: chk_daily_bias_clear ------------------------------------------

def test_bias_pass_when_stack_matches_direction() -> None:
    assert _by_key(_run(daily_bars=_uptrend_daily()))["chk_daily_bias_clear"].state == "pass"


def test_bias_fail_when_tangled() -> None:
    assert _by_key(_run(daily_bars=_chop_daily()))["chk_daily_bias_clear"].state == "fail"


def test_bias_fail_when_direction_contradicts() -> None:
    g = _run(direction="short", daily_bars=_uptrend_daily())
    assert _by_key(g)["chk_daily_bias_clear"].state == "fail"


def test_bias_unavailable_without_daily_bars() -> None:
    v = _by_key(_run(daily_bars=None))["chk_daily_bias_clear"]
    assert v.state == "unavailable" and "grade by eye" in v.fact


# ---- item 2: chk_daily_stack_ordered ---------------------------------------

def test_stack_ordered_pass_on_uptrend_long() -> None:
    assert _by_key(_run(daily_bars=_uptrend_daily()))["chk_daily_stack_ordered"].state == "pass"


def test_stack_ordered_fail_on_downtrend_long() -> None:
    assert _by_key(_run(daily_bars=_downtrend_daily()))["chk_daily_stack_ordered"].state == "fail"


def test_stack_ordered_unavailable_without_daily_bars() -> None:
    assert _by_key(_run())["chk_daily_stack_ordered"].state == "unavailable"


# ---- item 3: chk_m5_agrees -------------------------------------------------

def test_m5_pass_when_all_agree() -> None:
    g = _run(daily_bars=_uptrend_daily(), bars_5m=_uptrend_5m())
    assert _by_key(g)["chk_m5_agrees"].state == "pass"


def test_m5_fail_when_5m_disagrees() -> None:
    g = _run(daily_bars=_uptrend_daily(), bars_5m=_downtrend_5m())
    assert _by_key(g)["chk_m5_agrees"].state == "fail"


def test_m5_unavailable_when_too_few_bars() -> None:
    g = _run(daily_bars=_uptrend_daily(), bars_5m=_uptrend_5m(n=50))
    assert _by_key(g)["chk_m5_agrees"].state == "unavailable"


# ---- item 4: chk_gex_levels_marked -----------------------------------------

def test_levels_marked_pass_with_both_walls() -> None:
    assert _by_key(_run(snapshot=_snap()))["chk_gex_levels_marked"].state == "pass"


def test_levels_marked_null_flip_is_a_fact_not_a_failure() -> None:
    v = _by_key(_run(snapshot=_snap(gamma_flip=None)))["chk_gex_levels_marked"]
    assert v.state == "pass" and "flip" in v.fact


def test_levels_marked_fail_when_a_wall_is_null() -> None:
    v = _by_key(_run(snapshot=_snap(call_wall=None)))["chk_gex_levels_marked"]
    assert v.state == "fail"


def test_levels_marked_unavailable_when_snapshot_stale() -> None:
    v = _by_key(_run(snapshot=_snap(ts=datetime(2026, 7, 12, 10, 0))))["chk_gex_levels_marked"]
    assert v.state == "unavailable"


def test_levels_marked_unavailable_without_snapshot() -> None:
    assert _by_key(_run(snapshot=None))["chk_gex_levels_marked"].state == "unavailable"


# ---- item 6: chk_regime_match ----------------------------------------------

def test_regime_breakout_negative_passes() -> None:
    g = _run(play_type="breakout", snapshot=_snap(regime="negative"))
    assert _by_key(g)["chk_regime_match"].state == "pass"


def test_regime_range_positive_passes() -> None:
    g = _run(play_type="range", snapshot=_snap(regime="positive"))
    assert _by_key(g)["chk_regime_match"].state == "pass"


def test_regime_breakout_positive_fails() -> None:
    g = _run(play_type="breakout", snapshot=_snap(regime="positive"))
    assert _by_key(g)["chk_regime_match"].state == "fail"


def test_regime_unknown_fails() -> None:
    g = _run(play_type="breakout", snapshot=_snap(regime="unknown"))
    assert _by_key(g)["chk_regime_match"].state == "fail"


def test_regime_bad_play_type_needs_input() -> None:
    g = _run(play_type="", snapshot=_snap())
    assert _by_key(g)["chk_regime_match"].state == "needs_input"


def test_regime_unavailable_without_snapshot() -> None:
    assert _by_key(_run(snapshot=None))["chk_regime_match"].state == "unavailable"


# ---- item 8: chk_volume_confirming -----------------------------------------

def test_volume_pass_on_spike_with_body() -> None:
    g = _run(bars_5m=_uptrend_5m(last_vol=3_000_000.0))
    assert _by_key(g)["chk_volume_confirming"].state == "pass"


def test_volume_fail_when_below_multiple() -> None:
    g = _run(bars_5m=_uptrend_5m(last_vol=1_000_000.0))
    assert _by_key(g)["chk_volume_confirming"].state == "fail"


def test_volume_fail_when_body_against_direction() -> None:
    g = _run(bars_5m=_uptrend_5m(last_vol=3_000_000.0, last_green=False))
    assert _by_key(g)["chk_volume_confirming"].state == "fail"


def test_volume_unavailable_without_baseline() -> None:
    g = _run(bars_5m=_uptrend_5m(n=15))
    assert _by_key(g)["chk_volume_confirming"].state == "unavailable"


def test_volume_unavailable_without_bars() -> None:
    assert _by_key(_run(bars_5m=None))["chk_volume_confirming"].state == "unavailable"


# ---- item 11: chk_rr_at_least_2 --------------------------------------------

def test_rr_pass_when_ratio_clears_min() -> None:
    assert _by_key(_run(entry=100.0, stop=99.0, target=103.0))["chk_rr_at_least_2"].state == "pass"


def test_rr_fail_when_ratio_below_min_is_named() -> None:
    v = _by_key(_run(entry=100.0, stop=99.0, target=100.5))["chk_rr_at_least_2"]
    assert v.state == "fail"
    assert "R:R" in v.fact and "2.0" in v.fact


def test_rr_fail_on_side_insane_ordering() -> None:
    # long with stop above entry is not stop<entry<target
    v = _by_key(_run(entry=100.0, stop=101.0, target=103.0))["chk_rr_at_least_2"]
    assert v.state == "fail"


def test_rr_needs_input_until_levels_typed() -> None:
    v = _by_key(_run(entry=None, stop=None, target=None))["chk_rr_at_least_2"]
    assert v.state == "needs_input"


# ---- item 12: chk_confirmation_candle --------------------------------------

def test_confirmation_pass_on_green_completed_bar() -> None:
    g = _run(bars_5m=_uptrend_5m(last_green=True))
    assert _by_key(g)["chk_confirmation_candle"].state == "pass"


def test_confirmation_fail_on_red_completed_bar() -> None:
    g = _run(bars_5m=_uptrend_5m(last_green=False))
    assert _by_key(g)["chk_confirmation_candle"].state == "fail"


def test_confirmation_unavailable_without_bars() -> None:
    assert _by_key(_run(bars_5m=None))["chk_confirmation_candle"].state == "unavailable"


def test_in_progress_last_5m_bar_is_excluded() -> None:
    # last bar (18:10) is red and IN PROGRESS at 18:12 (< 18:15 close); the last
    # COMPLETED bar (18:05) is green -- item 12 must grade the completed one.
    bars = _uptrend_5m(n=105, last_green=False)
    in_progress = datetime(2026, 7, 13, 18, 12)
    g = _run(bars_5m=bars, now=in_progress)
    v = _by_key(g)["chk_confirmation_candle"]
    assert v.state == "pass"
    assert "18:05" in v.fact  # the completed bar, not the 18:10 in-progress one
    # once the 18:10 bar completes, its red body flips the item to fail
    after = datetime(2026, 7, 13, 18, 16)
    assert _by_key(_run(bars_5m=bars, now=after))["chk_confirmation_candle"].state == "fail"


# ---- verdict precedence ----------------------------------------------------

def test_full_ticket_is_yes() -> None:
    g = _run(daily_bars=_uptrend_daily(), bars_5m=_uptrend_5m(), snapshot=_snap(),
             play_type="breakout", entry=100.0, stop=99.0, target=103.0)
    assert all(v.state == "pass" for v in g.items)
    assert g.machine_verdict == "yes"


def test_named_facts_no_case() -> None:
    g = _run(daily_bars=_uptrend_daily(), bars_5m=_uptrend_5m(), snapshot=_snap(),
             entry=100.0, stop=99.0, target=100.5)  # R:R 0.5 < 2
    assert g.machine_verdict == "no"
    assert _by_key(g)["chk_rr_at_least_2"].state == "fail"


def test_incomplete_when_no_fail_but_gaps() -> None:
    # everything the machine can see passes; missing 5m + snapshot leave gaps
    g = _run(daily_bars=_uptrend_daily(), bars_5m=None, snapshot=None,
             entry=None, stop=None, target=None)
    states = {v.state for v in g.items}
    assert "fail" not in states
    assert g.machine_verdict == "incomplete"


def test_fail_beats_incomplete() -> None:
    # tangled daily is a hard NO even though 5m/snapshot are missing (unavailable)
    g = _run(daily_bars=_chop_daily(), bars_5m=None, snapshot=None,
             entry=None, stop=None, target=None)
    assert _by_key(g)["chk_daily_bias_clear"].state == "fail"
    assert g.machine_verdict == "no"


# ---- hints -----------------------------------------------------------------

def test_pivot_hint_flags_price_at_a_level() -> None:
    g = _run(entry=154.9, snapshot=_snap(call_wall=155.0))
    assert "at the pivot" in g.hints[0]


def test_pivot_hint_flags_mid_range() -> None:
    g = _run(entry=140.0, snapshot=_snap())
    assert "not at a pivot" in g.hints[0]


def test_pivot_hint_honest_when_no_levels() -> None:
    g = _run(entry=100.0, pivot_level=None, snapshot=None)
    assert "no" in g.hints[0].lower()


def test_stop_hint_reads_behind_structure() -> None:
    g = _run(direction="long", stop=147.0, snapshot=_snap(put_wall=148.0),
             bars_5m=_uptrend_5m())
    assert "behind structure" in g.hints[1]


def test_stop_hint_honest_when_nothing_to_measure() -> None:
    g = _run(stop=99.0, snapshot=None, bars_5m=None)
    assert "no protective level" in g.hints[1]

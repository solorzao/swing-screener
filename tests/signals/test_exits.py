import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.signals.exits import evaluate_exit, OpenTrade
from swing_screener.signals.frame import build_frame

CFG = StrategyConfig()
TRADE = OpenTrade(entry=100.0, stop=95.0, target=110.0, timeframe="1d", bars_held=3)


def _bar(close, high, low, bearish=False, shaved_head=False):
    return {"close": close, "ha_high": high, "ha_low": low,
            "low": low, "high": high, "bearish": bearish, "shaved_head": shaved_head}


def test_hard_stop_overrides_even_with_target_hit():
    bar = _bar(close=112, high=112, low=94)  # spiked to target but also broke stop
    d = evaluate_exit(TRADE, bar, CFG)
    assert d.action == "EXIT"
    assert d.tier == "hard"
    assert d.reason == "stop"


def test_momentum_flip_strong():
    bar = _bar(close=104, high=105, low=103, bearish=True, shaved_head=True)
    d = evaluate_exit(TRADE, bar, CFG)
    assert d.action == "EXIT" and d.tier == "strong" and d.reason == "momentum_flip"


def test_momentum_flip_disabled_by_config_holds():
    """With momentum_flip_exit=False the bearish HA flip no longer exits; the trade holds
    (only stop/target/time can close it). The no_flip experiment arm uses this."""
    cfg = StrategyConfig(momentum_flip_exit=False)
    bar = _bar(close=104, high=105, low=103, bearish=True, shaved_head=True)
    assert evaluate_exit(TRADE, bar, cfg).action == "HOLD"


def test_momentum_flip_disabled_still_stops_and_targets():
    """Disabling the flip must not disable the hard stop or the target. With the flip ON a
    shaved_head bar that also hit the target would exit as momentum_flip; with it OFF the
    same bar exits as target."""
    cfg = StrategyConfig(momentum_flip_exit=False)
    assert evaluate_exit(
        TRADE, _bar(close=96, high=97, low=94, shaved_head=True), cfg).reason == "stop"
    assert evaluate_exit(
        TRADE, _bar(close=111, high=111, low=108, shaved_head=True), cfg).reason == "target"


def test_momentum_flip_suppressed_for_reversal_by_default():
    """A reversal trade uses reversal_momentum_flip_exit (default False): the bearish HA flip
    no longer cuts the trade, so it holds. Continuation (default flip ON) is unaffected."""
    cfg = StrategyConfig()  # momentum_flip_exit=True, reversal_momentum_flip_exit=False
    bar = _bar(close=104, high=105, low=103, bearish=True, shaved_head=True)
    rev = OpenTrade(entry=100, stop=95, target=110, timeframe="1d", bars_held=3,
                    play_type="reversal")
    assert evaluate_exit(rev, bar, cfg).action == "HOLD"
    cont = OpenTrade(entry=100, stop=95, target=110, timeframe="1d", bars_held=3,
                     play_type="continuation")
    assert evaluate_exit(cont, bar, cfg).reason == "momentum_flip"


def test_reversal_momentum_flip_can_be_re_enabled():
    """The reversal flip is reversible: with reversal_momentum_flip_exit=True a reversal trade
    cuts on the flip again."""
    cfg = StrategyConfig(reversal_momentum_flip_exit=True)
    bar = _bar(close=104, high=105, low=103, bearish=True, shaved_head=True)
    rev = OpenTrade(entry=100, stop=95, target=110, timeframe="1d", bars_held=3,
                    play_type="reversal")
    assert evaluate_exit(rev, bar, cfg).reason == "momentum_flip"


def test_target_advisory():
    bar = _bar(close=111, high=111, low=108)
    d = evaluate_exit(TRADE, bar, CFG)
    assert d.action == "EXIT" and d.tier == "advisory" and d.reason == "target"


def test_time_stop_advisory():
    late = OpenTrade(entry=100, stop=95, target=110, timeframe="1d",
                     bars_held=CFG.max_hold_bars["1d"] + 1)
    d = evaluate_exit(late, _bar(close=101, high=102, low=100), CFG)
    assert d.action == "EXIT" and d.tier == "advisory" and d.reason == "time_stop"


def test_hold_when_nothing_triggers():
    d = evaluate_exit(TRADE, _bar(close=102, high=103, low=99), CFG)
    assert d.action == "HOLD"


def test_unknown_timeframe_disables_time_stop():
    # unknown tf -> max_hold_bars.get(tf, 0) == 0 -> the `if limit` guard must
    # prevent a spurious time-stop exit even at a huge bars_held.
    trade = OpenTrade(entry=100, stop=95, target=110, timeframe="unknown", bars_held=999)
    d = evaluate_exit(trade, _bar(close=101, high=102, low=99), CFG)
    assert d.action == "HOLD"


def test_evaluate_exit_accepts_real_frame_row():
    # a real enriched frame row is a pd.Series; evaluate_exit must handle it
    df = pd.DataFrame(
        {"open": [10, 11, 12], "high": [11, 12, 13], "low": [9, 10, 11],
         "close": [10.5, 11.5, 12.5], "volume": [1_000_000] * 3},
        index=pd.date_range("2024-01-01", periods=3, freq="D"),
    )
    row = build_frame(df, CFG).iloc[-1]
    trade = OpenTrade(entry=12.0, stop=10.5, target=20.0, timeframe="1d", bars_held=1)
    d = evaluate_exit(trade, row, CFG)
    assert d.action in ("EXIT", "HOLD")  # runs without KeyError/TypeError on a Series

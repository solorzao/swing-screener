from swing_screener.config import StrategyConfig
from swing_screener.signals.exits import evaluate_exit, OpenTrade

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

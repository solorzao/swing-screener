"""Tier-A continuation quality gates (the edge-tournament round-1 levers).

Each gate is a detection-only StrategyConfig lever, default no-op. Tests verify the gate
rejects when its condition is violated and the base still fires when it is off / satisfied.
Fixture trigger-bar values (probed): atr_pct=0.022, rsi=83.7, macd_hist<0 (rising),
body_frac=0.26, sep_atr=7.7, swing_low(std)=66.1 > ema_fast=63.26 (did NOT reach band).
"""

from swing_screener.config import StrategyConfig
from swing_screener.signals.detect import detect_last_bar
from swing_screener.signals.frame import build_frame


def _std_rows(trigger_vol=1_000_000, base_vol=1_000_000):
    """Uptrend + 3-bar shaved-head pullback + strong green trigger; volume on every bar."""
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0, "volume": base_vol})
        p += 1.0
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2, "volume": base_vol})
        p -= 1.2
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6, "volume": trigger_vol})
    return rows


def _deep_rows():
    """Deeper 4-bar pullback that reaches into the EMA20-50 band (swing_low ~61.7)."""
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(4):
        rows.append({"open": p, "high": p + 0.05, "low": p - 2.3, "close": p - 2.0})
        p -= 2.0
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})
    return rows


def _fires(rows, cfg, bars):
    return detect_last_bar(build_frame(bars(rows), cfg), cfg) is not None


def test_base_fixture_fires_with_all_gates_off(bars):
    assert _fires(_std_rows(), StrategyConfig(), bars)


def test_vol_thrust_gate(bars):
    # trigger volume 3x base -> rvol=3.0
    hi = _std_rows(trigger_vol=3_000_000)
    assert _fires(hi, StrategyConfig(vol_thrust_min=1.3), bars)          # 3.0 >= 1.3
    assert not _fires(hi, StrategyConfig(vol_thrust_min=4.0), bars)      # 3.0 < 4.0
    flat = _std_rows(trigger_vol=1_000_000)                              # rvol=1.0
    assert not _fires(flat, StrategyConfig(vol_thrust_min=1.3), bars)


def test_ema_separation_gate(bars):
    frame = build_frame(bars(_std_rows()), StrategyConfig())
    last = frame.iloc[-1]
    sep = (last["ema_fast"] - last["ema_slow"]) / last["atr"]
    assert sep > 1.0
    assert detect_last_bar(frame, StrategyConfig(min_ema_sep_atr=sep - 1)) is not None
    assert detect_last_bar(frame, StrategyConfig(min_ema_sep_atr=sep + 1)) is None


def test_macd_hook_gate(bars):
    # the synthetic fixture's trigger has macd_hist < 0, so the hook gate must reject it
    frame = build_frame(bars(_std_rows()), StrategyConfig())
    assert float(frame.iloc[-1]["macd_hist"]) < 0
    assert detect_last_bar(frame, StrategyConfig()) is not None
    assert detect_last_bar(frame, StrategyConfig(require_macd_hook=True)) is None


def test_rsi_floor_gate(bars):
    frame = build_frame(bars(_std_rows()), StrategyConfig())
    rsi = float(frame.iloc[-1]["rsi"])
    assert detect_last_bar(frame, StrategyConfig(rsi_min_trigger=rsi - 5)) is not None
    assert detect_last_bar(frame, StrategyConfig(rsi_min_trigger=rsi + 5)) is None


def test_min_atr_pct_gate(bars):
    frame = build_frame(bars(_std_rows()), StrategyConfig())
    last = frame.iloc[-1]
    atr_pct = last["atr"] / last["close"]
    assert detect_last_bar(frame, StrategyConfig(min_atr_pct=atr_pct - 0.005)) is not None
    assert detect_last_bar(frame, StrategyConfig(min_atr_pct=atr_pct + 0.005)) is None


def test_strong_flip_body_gate(bars):
    frame = build_frame(bars(_std_rows()), StrategyConfig())
    body = float(frame.iloc[-1]["body_frac"])
    # isolate the body threshold by making the lower-wick condition always pass
    assert detect_last_bar(
        frame, StrategyConfig(min_trigger_body_frac=body - 0.05,
                              max_trigger_lower_wick_frac=1.0)) is not None
    assert detect_last_bar(
        frame, StrategyConfig(min_trigger_body_frac=body + 0.10,
                              max_trigger_lower_wick_frac=1.0)) is None


def test_value_band_gate(bars):
    # std pullback stays above EMA20 (never reached the band) -> rejected
    assert not _fires(_std_rows(), StrategyConfig(require_value_band=True), bars)
    # deep pullback reaches into the EMA20-50 band -> passes
    assert _fires(_deep_rows(), StrategyConfig(require_value_band=True), bars)


def test_orderly_pullback_gate(bars):
    rows = _std_rows()
    # std pullback bars are orderly (<1.5 ATR) -> passes at defaults
    assert _fires(rows, StrategyConfig(require_orderly_pullback=True), bars)
    # an impossibly tight per-bar cap proves the gate measures bar range and rejects
    assert not _fires(rows, StrategyConfig(require_orderly_pullback=True,
                                           max_pullback_bar_atr=0.5), bars)

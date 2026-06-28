"""Relative-strength-vs-SPY leadership gate for the reversal book (edge-discovery wave 2).

When build_frame is given the SPY close, it adds an ``rs = close / spy_close`` column; the
reversal RS-leader gate then requires the RS line to be above its own MA at the bounce (buy
oversold names HOLDING UP vs the index, not the weakest laggards). No-op when rs is absent.
"""

from dataclasses import replace

import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.signals.frame import build_frame
from swing_screener.signals.reversal import detect_reversal

CFG = StrategyConfig()


def _bar(o, h, low, c, v=1_000_000.0):
    return {"open": o, "high": h, "low": low, "close": c, "volume": v}


def _reversal_raw():
    """Flat base, decline below the slow EMA, heavy-volume green flip -> a reversal."""
    rows, p = [], 100.0
    for i in range(46):
        o = p
        c = p + (0.5 if i % 2 else -0.5)
        rows.append(_bar(o, max(o, c) + 0.3, min(o, c) - 0.3, c))
        p = c
    for _ in range(14):
        o = p
        c = p - 2.2
        rows.append(_bar(o, o + 0.2, c - 0.3, c))
        p = c
    rows.append(_bar(p + 0.2, p + 12.5, p - 0.1, p + 12.0, v=3_000_000.0))
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx)


def _spy(index, *, start, end):
    return pd.Series([start + (end - start) * i / (len(index) - 1) for i in range(len(index))],
                     index=index)


def test_build_frame_adds_rs_only_when_spy_given():
    raw = _reversal_raw()
    spy = _spy(raw.index, start=100.0, end=100.0)
    with_rs = build_frame(raw, CFG, spy_close=spy)
    assert "rs" in with_rs.columns
    assert with_rs["rs"].iloc[-1] == raw["close"].iloc[-1] / 100.0
    assert "rs" not in build_frame(raw, CFG).columns


def test_rs_leader_gate_passes_leaders_rejects_laggards():
    raw = _reversal_raw()
    # leading name: SPY falls FASTER than the name -> rs (name/spy) rises into the bounce
    lead = build_frame(raw, CFG, spy_close=_spy(raw.index, start=100.0, end=40.0))
    # lagging name: SPY rises while the name declines -> rs falls into the bounce
    lag = build_frame(raw, CFG, spy_close=_spy(raw.index, start=100.0, end=140.0))
    cfg = replace(CFG, require_rs_leader=True)

    rs_lead = lead["rs"]
    rs_lag = lag["rs"]
    # preconditions: the leader's RS is above its MA at the bounce; the laggard's is below
    assert rs_lead.iloc[-1] > rs_lead.tail(cfg.rs_ma_window).mean()
    assert rs_lag.iloc[-1] < rs_lag.tail(cfg.rs_ma_window).mean()

    assert detect_reversal(lead, cfg) is not None
    assert detect_reversal(lag, cfg) is None


def test_rs_gate_is_noop_without_rs_column():
    # gate on, but no rs column (SPY not provided) -> fail open (still fires)
    raw = _reversal_raw()
    frame = build_frame(raw, CFG)
    assert "rs" not in frame.columns
    assert detect_reversal(frame, replace(CFG, require_rs_leader=True)) is not None

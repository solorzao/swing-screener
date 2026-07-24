"""Tests for the per-timeframe deterministic ticker read (OD Task 3).

Pure / offline: builds enriched frames with build_frames from the bars fixture --
one engineered to fire a continuation signal, one flat -- and asserts the read
fields are sensible and the firing frame attaches a setup while the flat one does
not.
"""

from swing_screener.config import StrategyConfig
from swing_screener.notify.ticker_report import (
    TimeframeRead,
    build_ticker_reads,
)
from swing_screener.pipeline.analyze import SignalResult, build_frames


def _firing(bars):
    """A clean uptrend, a three-bar pullback, then a strong green flip (fires)."""
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})
    return bars(rows)


def _flat(bars):
    """A dead-flat series -- no trend, no trigger, no setup."""
    return bars([{"open": 50.0, "high": 50.1, "low": 49.9, "close": 50.0} for _ in range(60)])


def test_build_ticker_reads_attaches_setup_on_firing_frame(bars):
    # the synthetic _firing trigger runs past the freshness gate; this test is about
    # the report attaching a setup, so disable the anti-chase gate here.
    cfg = StrategyConfig(max_extension_atr=0.0)
    frames = build_frames({"1d": _firing(bars)}, cfg)

    reads = build_ticker_reads("AAPL", frames, cfg)

    assert len(reads) == 1
    read = reads[0]
    assert isinstance(read, TimeframeRead)
    assert read.timeframe == "1d"
    assert read.ha_trend in {"bullish", "bearish"}
    assert isinstance(read.ema_aligned, bool)
    assert isinstance(read.rsi, float)
    assert isinstance(read.atr_pct, float)
    assert read.atr_pct > 0.0
    # the firing frame attaches a concrete setup with real levels
    assert isinstance(read.setup, SignalResult)
    assert read.setup.timeframe == "1d"
    assert read.setup.stop < read.setup.target


def test_build_ticker_reads_leaves_setup_none_on_flat_frame(bars):
    cfg = StrategyConfig()
    frames = build_frames({"1d": _flat(bars)}, cfg)

    reads = build_ticker_reads("FLAT", frames, cfg)

    assert len(reads) == 1
    read = reads[0]
    assert read.timeframe == "1d"
    assert read.setup is None
    assert read.ha_trend in {"bullish", "bearish"}


def test_build_ticker_reads_iterates_canonical_order_and_skips_missing(bars):
    cfg = StrategyConfig()
    # provide 1d and 4h out of canonical order; only present frames are read,
    # and they come back in canonical low->high order (4h before 1d).
    frames = build_frames({"1d": _flat(bars), "4h": _flat(bars)}, cfg)

    reads = build_ticker_reads("MULTI", frames, cfg)

    assert [r.timeframe for r in reads] == ["4h", "1d"]

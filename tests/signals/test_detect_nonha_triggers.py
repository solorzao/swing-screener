"""Non-HA continuation triggers (Q9): fire nearer the pullback low.

D1 measured that the shipped HA-flip entry pays a median 1.56 ATR above the setup's own
low against 1.81 ATR of total risk -- ~86% of every R risked is give-back. Heiken-Ashi is
smoothed, so a bullish flip CANNOT fire at the low; it fires ~2 bars later. These raw-candle
trigger kinds attack that directly.

Everything except the trigger test stays byte-identical to the shipped detector: the same
uptrend precondition, the same HA-based pullback walk-back and shaved-head requirement, the
same shallow-pullback gate and zone geometry. Only WHICH BAR fires changes.

See docs/plans/2026-08-16-nonha-trigger-preregistration.md.
"""

import pytest

from swing_screener.config import StrategyConfig
from swing_screener.signals.detect import detect_last_bar
from swing_screener.signals.frame import build_frame


def _uptrend_pullback() -> list[dict]:
    """A clean uptrend then a 3-bar shaved-head pullback. The LAST row is the final
    pullback bar; callers append their own trigger bar."""
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    return rows


def _fires(bars, rows: list[dict], kind: str) -> bool:
    cfg = StrategyConfig(trigger_kind=kind)
    return detect_last_bar(build_frame(bars(rows), cfg), cfg) is not None


# ---------------------------------------------------------------------------
# raw_up: the earliest possible trigger -- any raw up bar off the pause.
# ---------------------------------------------------------------------------
def test_raw_up_fires_on_a_modest_up_bar_that_no_ha_flip_would_catch(bars) -> None:
    """THE point of the experiment. A small up bar leaves the smoothed HA series still
    bearish, so the shipped trigger stays silent and only fires bars later -- by which
    time price has left the low. raw_up fires here."""
    rows = _uptrend_pullback()
    prev = rows[-1]
    rows.append({"open": prev["close"], "high": prev["close"] + 0.4,
                 "low": prev["close"] - 0.1, "close": prev["close"] + 0.3})

    assert _fires(bars, rows, "raw_up") is True
    assert _fires(bars, rows, "ha_flip") is False   # the shipped trigger is still waiting


def test_raw_up_does_not_fire_on_a_down_bar(bars) -> None:
    rows = _uptrend_pullback()
    prev = rows[-1]
    rows.append({"open": prev["close"], "high": prev["close"] + 0.2,
                 "low": prev["close"] - 1.0, "close": prev["close"] - 0.8})

    assert _fires(bars, rows, "raw_up") is False


# ---------------------------------------------------------------------------
# raw_reclaim: close up AND above the prior bar's high.
# ---------------------------------------------------------------------------
def test_raw_reclaim_needs_the_close_above_the_prior_high(bars) -> None:
    """An up bar that stays inside the prior bar's range is not a reclaim -- it is the
    pause continuing. raw_up takes it; raw_reclaim must not."""
    rows = _uptrend_pullback()
    prev = rows[-1]
    inside = prev["high"] - 0.02          # up, but still below the prior high
    rows.append({"open": prev["close"], "high": prev["high"] - 0.01,
                 "low": prev["close"] - 0.1, "close": inside})

    assert _fires(bars, rows, "raw_up") is True
    assert _fires(bars, rows, "raw_reclaim") is False


def test_raw_reclaim_fires_when_the_close_clears_the_prior_high(bars) -> None:
    rows = _uptrend_pullback()
    prev = rows[-1]
    rows.append({"open": prev["close"], "high": prev["high"] + 1.0,
                 "low": prev["close"] - 0.1, "close": prev["high"] + 0.6})

    assert _fires(bars, rows, "raw_reclaim") is True


# ---------------------------------------------------------------------------
# raw_reclaim_hl: reclaim PLUS a higher low (a structural turn, not a spike).
# ---------------------------------------------------------------------------
def test_raw_reclaim_hl_rejects_a_reclaim_that_undercut_the_prior_low(bars) -> None:
    """A bar that spikes below the prior low and closes strong is a reversal-shaped bar,
    not a continuation turn -- and its own low sits under the swing low the stop is
    anchored to. raw_reclaim takes it; raw_reclaim_hl must not."""
    rows = _uptrend_pullback()
    prev = rows[-1]
    rows.append({"open": prev["close"], "high": prev["high"] + 1.0,
                 "low": prev["low"] - 0.5, "close": prev["high"] + 0.6})

    assert _fires(bars, rows, "raw_reclaim") is True
    assert _fires(bars, rows, "raw_reclaim_hl") is False


def test_raw_reclaim_hl_fires_on_a_reclaim_that_held_a_higher_low(bars) -> None:
    rows = _uptrend_pullback()
    prev = rows[-1]
    rows.append({"open": prev["close"], "high": prev["high"] + 1.0,
                 "low": prev["low"] + 0.3, "close": prev["high"] + 0.6})

    assert _fires(bars, rows, "raw_reclaim_hl") is True


# ---------------------------------------------------------------------------
# Everything OTHER than the trigger test must stay identical.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["raw_up", "raw_reclaim", "raw_reclaim_hl"])
def test_non_ha_triggers_still_require_the_uptrend_precondition(bars, kind: str) -> None:
    """A downtrend must reject regardless of how bullish the last bar looks: only the
    trigger changes, never the context the trigger fires in."""
    rows = [{"open": 50 - i, "high": 51 - i, "low": 49 - i, "close": 50 - i}
            for i in range(60)]
    prev = rows[-1]
    rows.append({"open": prev["close"], "high": prev["high"] + 3.0,
                 "low": prev["low"], "close": prev["high"] + 2.5})

    assert _fires(bars, rows, kind) is False


@pytest.mark.parametrize("kind", ["raw_up", "raw_reclaim", "raw_reclaim_hl"])
def test_non_ha_triggers_still_require_a_pullback(bars, kind: str) -> None:
    """An uninterrupted up-run has no pause to resume from; every kind must reject."""
    rows = [{"open": 10 + i, "high": 11 + i, "low": 10 + i, "close": 11 + i}
            for i in range(62)]

    assert _fires(bars, rows, kind) is False


def test_the_default_trigger_kind_is_unchanged(bars) -> None:
    """The shipped default must keep firing exactly where it did -- this whole experiment
    is off-by-default until a pre-registered walk says otherwise."""
    rows = _uptrend_pullback()
    p = rows[-1]["close"]
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})
    cfg = StrategyConfig()

    assert cfg.trigger_kind == "ha_flip"
    assert detect_last_bar(build_frame(bars(rows), cfg), cfg) is not None

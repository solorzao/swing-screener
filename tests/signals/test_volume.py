"""Tests for the SHARED volume-footprint arithmetic (``signals.volume``).

These functions are the single definition behind two consumers: the (default-off)
volume gates in ``signals.detect`` and the volume FACTS the conviction analyst reads.
Extracting them is what makes a reported number and a future gate agree by
construction instead of by coincidence.

The NaN discipline is deliberately asymmetric and is the subtlest thing here:

* the pullback mean is ``sum(...)/k``, which PROPAGATES NaN -- pandas' ``.mean()``
  would skip it. The gates rely on that: an unmeasurable dry-up must REJECT (the
  2026-07 audit fix), and a NaN comparison is False, so a skipped NaN would silently
  no-op the gate.
* the profile REPORTS ``None`` for an unmeasurable term rather than a neutral 1.0.
  Fabricating 1.0 would tell the analyst "volume was average" when the truth is
  "volume was unknown" -- index tickers keep NaN volume rows at the download seam.
"""

import math

import pandas as pd

from swing_screener.signals.volume import (
    VolumeProfile,
    is_pocket_pivot,
    pre_setup_baseline,
    pullback_dryup,
    volume_profile,
    volume_thrust,
)


def _frame(volumes, closes=None, opens=None):
    """A minimal OHLCV frame; only volume/close/open matter to these functions."""
    n = len(volumes)
    closes = closes if closes is not None else [10.0] * n
    opens = opens if opens is not None else [9.0] * n
    return pd.DataFrame({"volume": volumes, "close": closes, "open": opens})


# ---------------------------------------------------------------------------
# pre_setup_baseline: the window BEFORE the setup, excluding the trigger bar.
# ---------------------------------------------------------------------------
def test_pre_setup_baseline_excludes_the_setup_window_and_the_trigger_bar() -> None:
    """3 baseline bars of 100, then a 2-bar pullback at 10, then the trigger at 999.
    Only the 100s may count -- including the dried-up pullback in its own denominator
    is what flattered thrust ratios on longer pullbacks."""
    f = _frame([100.0, 100.0, 100.0, 10.0, 10.0, 999.0])
    assert pre_setup_baseline(f, window=3, setup_bars=2) == 100.0


def test_pre_setup_baseline_with_no_setup_bars_is_the_legacy_window() -> None:
    """setup_bars=0 reproduces the legacy (inclusive) thrust denominator exactly: the
    ``window`` bars immediately before the trigger."""
    f = _frame([100.0, 100.0, 10.0, 10.0, 999.0])
    assert pre_setup_baseline(f, window=4, setup_bars=0) == 55.0  # (100+100+10+10)/4


# ---------------------------------------------------------------------------
# volume_thrust / pullback_dryup.
# ---------------------------------------------------------------------------
def test_volume_thrust_is_trigger_over_baseline() -> None:
    assert volume_thrust(180.0, 100.0) == 1.8


def test_pullback_dryup_below_one_means_supply_exhausting() -> None:
    assert pullback_dryup([60.0, 60.0], 100.0) == 0.6


def test_a_non_positive_baseline_reads_as_neutral_not_as_a_divide_by_zero() -> None:
    """Matches the gates: an absent/zero denominator yields a neutral 1.0 rather than
    raising or producing an infinity that would silently pass every threshold."""
    assert volume_thrust(180.0, 0.0) == 1.0
    assert pullback_dryup([60.0], 0.0) == 1.0


def test_a_nan_pullback_bar_propagates_so_the_gate_still_rejects() -> None:
    """sum()/k, NOT pandas .mean(): a skipped NaN would produce a real number and let an
    unmeasurable dry-up pass the gate."""
    assert math.isnan(pullback_dryup([60.0, float("nan")], 100.0))


# ---------------------------------------------------------------------------
# is_pocket_pivot: demand exceeds the worst recent supply.
# ---------------------------------------------------------------------------
def test_pocket_pivot_when_the_up_bar_outvolumes_every_recent_down_day() -> None:
    f = _frame(volumes=[50.0, 80.0, 200.0], closes=[9.0, 9.0, 11.0],
               opens=[10.0, 10.0, 10.0])   # two down bars, then an up bar on 200
    assert is_pocket_pivot(f, lookback=2) is True


def test_not_a_pocket_pivot_when_a_down_day_traded_heavier() -> None:
    f = _frame(volumes=[50.0, 300.0, 200.0], closes=[9.0, 9.0, 11.0],
               opens=[10.0, 10.0, 10.0])
    assert is_pocket_pivot(f, lookback=2) is False


def test_not_a_pocket_pivot_when_the_trigger_bar_itself_closed_down() -> None:
    f = _frame(volumes=[50.0, 80.0, 200.0], closes=[9.0, 9.0, 9.5],
               opens=[10.0, 10.0, 10.0])   # heavy, but a DOWN bar: not demand
    assert is_pocket_pivot(f, lookback=2) is False


# ---------------------------------------------------------------------------
# volume_profile: the reported facts.
# ---------------------------------------------------------------------------
def test_volume_profile_reports_the_three_facts() -> None:
    f = _frame(volumes=[100.0, 100.0, 100.0, 60.0, 60.0, 180.0],
               closes=[10.0] * 5 + [11.0], opens=[10.0] * 5 + [10.0])
    prof = volume_profile(f, setup_bars=2, window=3, pocket_lookback=2)

    assert prof == VolumeProfile(rvol_trigger=1.8, rvol_pullback=0.6, pocket_pivot=True)


def test_volume_profile_reports_none_rather_than_fabricating_a_neutral_read() -> None:
    """NaN volume (index tickers keep those rows at the download seam) must surface as
    "not measured", never as 1.0 -- the analyst would read a fabricated 1.0 as evidence
    that volume was unremarkable."""
    f = _frame(volumes=[100.0, 100.0, 100.0, float("nan"), 60.0, 180.0])
    prof = volume_profile(f, setup_bars=2, window=3, pocket_lookback=2)

    assert prof.rvol_pullback is None
    assert prof.rvol_trigger == 1.8      # the measurable term still reports


def test_volume_profile_is_all_none_on_a_frame_too_short_to_measure() -> None:
    """A frame shorter than the baseline window has no denominator to speak of; every
    term must degrade to None instead of raising inside a live screen."""
    prof = volume_profile(_frame([100.0, 120.0]), setup_bars=2, window=20,
                          pocket_lookback=2)

    assert prof == VolumeProfile(rvol_trigger=None, rvol_pullback=None, pocket_pivot=None)

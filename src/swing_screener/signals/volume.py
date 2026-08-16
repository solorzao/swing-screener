"""The volume footprint of a setup: ONE definition, two consumers.

The same three quantities answer the two questions the system asks about volume:

* the (default-off) GATES in :mod:`swing_screener.signals.detect` -- ``vol_thrust_min``,
  ``pullback_vol_dryup_max``, ``require_pocket_pivot`` -- which REJECT a trigger; and
* the volume FACTS the conviction analyst reads, which merely INFORM its +-1 nudge.

They live here rather than being computed twice so a reported number and a gate
threshold cannot drift apart: the digest can never tell the analyst "pullback 0.62x"
while a gate computes 0.71x from a different window.

Discipline: nothing here gates anything by itself. "Volume dry-up" and "pocket pivot"
are still UNRUN experiments on the edge-discovery backlog, so the profile is reported
to the analyst as context and is deliberately absent from ``conviction_baseline`` and
from the ranking score. It earns teeth only by certifying, on the record the analyst's
own nudges build.

NaN discipline differs between the two consumers, on purpose:

* the pullback mean is ``sum(...)/k``, which PROPAGATES NaN, because the gates must
  REJECT an unmeasurable dry-up (2026-07 audit: a NaN comparison is False, so a
  skipped NaN silently no-ops the gate). ``pandas.mean()`` would skip it.
* :func:`volume_profile` REPORTS ``None`` for an unmeasurable term. A fabricated 1.0
  would read to the analyst as "volume was average" when the truth is "volume was
  unknown" -- index tickers keep NaN volume rows at the download seam.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class VolumeProfile:
    """The three reported volume facts for one setup. ``None`` == not measurable.

    ``rvol_trigger`` -- trigger-bar volume over the pre-setup baseline (demand
    returning). ``rvol_pullback`` -- the setup window's mean over that SAME baseline,
    so the two are directly comparable; below 1.0 means supply exhausted into the
    trigger, the constructive shape. ``pocket_pivot`` -- the self-normalizing form:
    did the up trigger bar out-trade every recent down day?
    """

    rvol_trigger: float | None
    rvol_pullback: float | None
    pocket_pivot: bool | None


def pre_setup_baseline(f: pd.DataFrame, *, window: int, setup_bars: int) -> float:
    """Mean volume over the ``window`` bars ENDING just before the setup window.

    Excluding the setup's own dried-up bars is the point: a baseline that includes them
    is depressed by exactly the contraction being measured against it, which flatters
    thrust ratios on longer pullbacks. ``setup_bars=0`` reproduces the legacy inclusive
    denominator (the ``vol_thrust_excl_pullback=False`` arm).
    """
    return float(f["volume"].iloc[-(window + 1 + setup_bars):-(setup_bars + 1)].mean())


def volume_thrust(trigger_volume: float, baseline: float) -> float:
    """Trigger-bar volume relative to ``baseline``. A non-positive baseline reads as a
    neutral 1.0 rather than raising or yielding an infinity that would pass any gate."""
    return trigger_volume / baseline if baseline and baseline > 0 else 1.0


def pullback_dryup(setup_volumes: Sequence[float], baseline: float) -> float:
    """The setup window's mean volume relative to ``baseline`` (< 1.0 == contraction).

    The mean is ``sum(...)/k`` so a NaN bar PROPAGATES -- see the module docstring: the
    gates depend on an unmeasurable dry-up staying unmeasurable.
    """
    if not setup_volumes or not baseline or baseline <= 0:
        return 1.0
    return sum(setup_volumes) / len(setup_volumes) / baseline


def is_pocket_pivot(f: pd.DataFrame, *, lookback: int) -> bool:
    """True iff the last bar closed UP on more volume than any down day in the prior
    ``lookback`` bars -- demand outweighing the worst recent supply, self-normalizing so
    it needs no baseline window at all."""
    last = f.iloc[-1]
    window = f.iloc[-(lookback + 1):-1]
    down = window.loc[window["close"] < window["open"], "volume"]
    down_vol_max = float(down.max()) if len(down) else 0.0
    return bool(float(last["close"]) > float(last["open"])
                and float(last["volume"]) > down_vol_max)


def _measured(value: float) -> float | None:
    """A finite reading, or None when it could not be measured."""
    return value if math.isfinite(value) else None


def volume_profile(f: pd.DataFrame, *, setup_bars: int, window: int,
                   pocket_lookback: int) -> VolumeProfile:
    """The reported volume facts for the setup ending at ``f``'s last bar.

    ``setup_bars`` is the pullback length for a continuation play and the decline window
    for a reversal, so both play types describe "the run-up into the trigger" the same
    way and the analyst can compare the two numbers it is given.

    Degrades to ``None`` rather than raising: a frame too short for the baseline window
    yields an empty slice (NaN mean), and any non-finite term reports as not measured.
    A live screen must never die on a thin history.
    """
    if len(f) < setup_bars + 2:
        return VolumeProfile(rvol_trigger=None, rvol_pullback=None, pocket_pivot=None)
    baseline = pre_setup_baseline(f, window=window, setup_bars=setup_bars)
    if not math.isfinite(baseline) or baseline <= 0:
        # No denominator -> the two ratios are unmeasurable. The pocket pivot needs no
        # baseline, so it is still a real reading and is kept.
        return VolumeProfile(rvol_trigger=None, rvol_pullback=None,
                             pocket_pivot=is_pocket_pivot(f, lookback=pocket_lookback))
    setup_volumes = [float(v) for v in f["volume"].iloc[-(setup_bars + 1):-1]]
    return VolumeProfile(
        rvol_trigger=_measured(volume_thrust(float(f.iloc[-1]["volume"]), baseline)),
        rvol_pullback=_measured(pullback_dryup(setup_volumes, baseline)),
        pocket_pivot=is_pocket_pivot(f, lookback=pocket_lookback),
    )

"""Experiment arms for the parallel-arm shadow book.

Every fill is opened once per arm (identical entry economics, different exit
management) and tagged with its arm name; ``advance_open`` then walks each open
trade forward under its own arm config. ``analytics.performance.breakdown(trades,
"arm")`` reads the resulting books back as a same-sample A/B -- an arm sees the
EXACT same tickers, fills, and dates as the baseline, so the comparison isn't
confounded by regime the way a temporal before/after would be.

Adding an arm is one entry here; nothing else in the pipeline changes.

**The opening roster is currently baseline-only.** The exit-policy question was asked
in full -- disable the momentum flip (``no_flip``), scale out a third conditionally
(``partial33_cond``), ride the remainder on a Chandelier trail (``partial33_chand``),
ratchet to breakeven at +1R (``be_1r``) -- and all four settled FUTILE against
baseline on 2026-08-15 (paired deltas between -0.015R and +0.003R on ~2,800 pairs
each, every upper bound an order of magnitude below the 0.10R MDE). Their registry
rows in ``edge/experiments.json`` keep the numbers and the reasoning; this roster
keeps only the control, per the retirement procedure in docs/cockpit.md. Exit
management is therefore a SETTLED question, not an open one: a new arm needs a new
hypothesis, not a re-run of these.

Three sets, deliberately distinct -- conflating them is how a retirement either
strands trades or quietly keeps accruing them:

* ``build_arms`` -- the OPENING roster. New fills are booked under these. The
  registry lockstep test matches this set.
* ``build_draining_arms`` -- retired arms with trades still open. Never opened
  under, always advanced.
* ``build_stepping_arms`` -- the union; what ``advance_open`` walks.
"""

from dataclasses import replace

from swing_screener.config import StrategyConfig

# The arm whose exits match the pre-overhaul behavior; the comparison baseline and
# the default tag for any trade that predates the dual-book.
BASELINE = "baseline"


def build_arms(base: StrategyConfig) -> dict[str, StrategyConfig]:
    """The OPENING roster: arms a new fill is booked under, derived from ``base``.

    ``baseline`` is pinned all-or-nothing via ``partial_frac=0.0`` so it stays the
    honest control even if ``base``'s default ever changes.

    Retiring the four exit arms leaves this a single-entry mapping: the shadow book
    books ONE row per fill per screen variant instead of five, which is the point --
    the arm multiplication was diluting forward accrual across books that had already
    answered their question.

    This is the set the registry lockstep test matches (tests/pipeline/test_registry.py).
    It is NOT the set the bar-stepper walks -- see ``build_stepping_arms``.
    """
    return {BASELINE: replace(base, partial_frac=0.0)}


def build_draining_arms(base: StrategyConfig) -> dict[str, StrategyConfig]:
    """Retired arms whose IN-FLIGHT fills must still be advanced to a natural close.

    ``advance_open`` refuses to guess an exit policy for an off-roster arm, so deleting
    a roster line while that arm still has open trades strands them: the 2026-08-15
    retirement left 4,242 open and 1,836 pending rows that would never have been stepped
    again. Draining keeps each retired config available to the STEPPER only -- no new
    fills are booked under it, but every fill already booked closes on the exact policy
    it was opened with, so the arm's final book is complete instead of truncated
    mid-flight.

    The configs below must stay BYTE-IDENTICAL to what the arm shipped with; advancing a
    trade under a policy it was not booked under would silently rewrite the experiment it
    already settled. They are frozen copies, deliberately not re-derived from anything
    that can drift.

    Remove an entry once its last row has closed -- the screen logs ``DRAIN_COMPLETE``
    for a draining arm with no open rows left, so the deletion is a signal rather than a
    calendar reminder. Every name here must carry a ``retired`` registry row.
    """
    return {
        "no_flip": replace(base, partial_frac=0.0, momentum_flip_exit=False),
        "partial33_cond": replace(base, partial_frac=0.33, partial_require_softening=True),
        "partial33_chand": replace(base, partial_frac=0.33, partial_require_softening=True,
                                   trail_mode="chandelier", chandelier_atr_mult=3.0),
        "be_1r": replace(base, partial_frac=0.0, breakeven_after_r=1.0),
    }


def build_stepping_arms(base: StrategyConfig) -> dict[str, StrategyConfig]:
    """Every arm the BAR-STEPPER must be able to advance: the opening roster plus the
    draining set. ``advance_open`` takes this; ``open_from_signals`` takes
    ``build_arms``. Keeping the two apart is what lets an arm stop accruing new fills
    without abandoning the ones it already has."""
    return {**build_arms(base), **build_draining_arms(base)}

"""Experiment arms for the parallel-arm shadow book.

Every fill is opened once per arm (identical entry economics, different exit
management) and tagged with its arm name; ``advance_open`` then walks each open
trade forward under its own arm config. ``analytics.performance.breakdown(trades,
"arm")`` reads the resulting books back as a same-sample A/B -- an arm sees the
EXACT same tickers, fills, and dates as the baseline, so the comparison isn't
confounded by regime the way a temporal before/after would be.

Adding an arm is one entry here; nothing else in the pipeline changes.

**The roster is currently baseline-only.** The exit-policy question was asked in
full -- disable the momentum flip (``no_flip``), scale out a third conditionally
(``partial33_cond``), ride the remainder on a Chandelier trail (``partial33_chand``),
ratchet to breakeven at +1R (``be_1r``) -- and all four settled FUTILE against
baseline on 2026-08-15 (paired deltas between -0.015R and +0.003R on ~2,800 pairs
each, every upper bound an order of magnitude below the 0.10R MDE). Their registry
rows in ``edge/experiments.json`` keep the numbers and the reasoning; this roster
keeps only the control, per the retirement procedure in docs/cockpit.md. Exit
management is therefore a SETTLED question, not an open one: a new arm needs a new
hypothesis, not a re-run of these.
"""

from dataclasses import replace

from swing_screener.config import StrategyConfig

# The arm whose exits match the pre-overhaul behavior; the comparison baseline and
# the default tag for any trade that predates the dual-book.
BASELINE = "baseline"


def build_arms(base: StrategyConfig) -> dict[str, StrategyConfig]:
    """The active experiment arms, derived from ``base``.

    ``baseline`` is pinned all-or-nothing via ``partial_frac=0.0`` so it stays the
    honest control even if ``base``'s default ever changes.

    Retiring the four exit arms leaves this a single-entry mapping: the shadow book
    books ONE row per fill per screen variant instead of five, which is the point --
    the arm multiplication was diluting forward accrual across books that had already
    answered their question. ``advance_open`` skips any open trade whose arm is no
    longer here (it will not guess an exit policy), so the retired arms' in-flight
    rows stay open and unstepped; they are decided experiments, so nothing is lost.
    """
    return {BASELINE: replace(base, partial_frac=0.0)}

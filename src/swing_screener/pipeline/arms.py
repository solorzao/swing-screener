"""Experiment arms for the parallel-arm shadow book.

Every fill is opened once per arm (identical entry economics, different exit
management) and tagged with its arm name; ``advance_open`` then walks each open
trade forward under its own arm config. ``analytics.performance.breakdown(trades,
"arm")`` reads the resulting books back as a same-sample A/B -- the partial arms
see the EXACT same tickers, fills, and dates as the baseline, so the comparison
isn't confounded by regime the way a temporal before/after would be.

Adding an arm is one entry here (e.g. the Step D Chandelier trail); nothing else
in the pipeline changes.
"""

from dataclasses import replace

from swing_screener.config import StrategyConfig

# The arm whose exits match the pre-overhaul behavior; the comparison baseline and
# the default tag for any trade that predates the dual-book.
BASELINE = "baseline"


def build_arms(base: StrategyConfig) -> dict[str, StrategyConfig]:
    """The active experiment arms, derived from ``base``.

    ``baseline`` is pinned all-or-nothing via ``partial_frac=0.0`` so it stays the
    honest control even if ``base``'s default ever changes; ``partial33_cond`` is
    the Step C conditional 33% scale-out (only when HA momentum is softening), and
    ``partial33_chand`` is the same partial but runs the post-partial remainder under
    a Chandelier trail instead of a static breakeven stop -- the Step D bake-off pits
    these two against each other (same partial, different runner management).

    ``no_flip`` mirrors ``baseline`` exactly except it disables the momentum-flip exit
    (``momentum_flip_exit=False``), so ``breakdown(trades, "arm")`` is a clean same-sample
    A/B of the flip -- letting the live book decide whether to retire it (the offline study
    found it a mild drag but couldn't confirm it out-of-sample).
    """
    return {
        BASELINE: replace(base, partial_frac=0.0),
        "no_flip": replace(base, partial_frac=0.0, momentum_flip_exit=False),
        "partial33_cond": replace(base, partial_frac=0.33, partial_require_softening=True),
        "partial33_chand": replace(base, partial_frac=0.33, partial_require_softening=True,
                                   trail_mode="chandelier", chandelier_atr_mult=3.0),
    }

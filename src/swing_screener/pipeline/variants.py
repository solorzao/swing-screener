"""Screen variants for the strategy leaderboard.

The orthogonal complement to ``arms``. Where an *arm* replays one fill under a
different EXIT policy (same-sample), a *variant* re-screens the prior bar under a
different ENTRY/screen config and paper-trades its own fills under the baseline exit.
``analytics.performance.breakdown(trades, "variant")`` (filtered to the baseline arm)
then ranks the screen configs head-to-head -- a strategy leaderboard.

Because the shadow book reuses the enriched frames built once with the base config,
variants MUST keep the base's INDICATOR periods (EMA/ATR/RSI/MACD/HA classification);
they may only differ in DETECTION / zone / scoring thresholds (e.g. ``max_extension_atr``,
``min_pullback_bars``, ``oversold_rsi_max``). ``build_screen_variants`` enforces that so a
mis-specified variant fails loudly instead of silently scoring against the wrong frame.
"""

from dataclasses import replace

from swing_screener.config import StrategyConfig

# The variant whose screen config IS the live one; the comparison baseline and the
# default tag for any paper trade that predates the variant dimension.
DEFAULT_VARIANT = "default"

# Fields that change the enriched frame (build_frame). A variant that touches any of
# these would need its own frames, which the shared-frame shadow book does not build.
_INDICATOR_FIELDS = (
    "ema_fast", "ema_slow", "atr_period", "rsi_period", "macd_fast", "macd_slow",
    "macd_signal", "wick_frac", "zone_body_frac",
)

# Fields the EXIT machinery reads (evaluate_exit / advance_open). A screen variant that
# differs from base in one of these is a SILENT NO-OP: both live (pipeline/run.py) and
# replay (pipeline/replay.py) advance every open trade under the BASE config's exits, so
# the variant books an identical book and the sweep honestly reports "no delta" -- a
# false null recorded as tested-and-dead when it was never tested (2026-07 review).
# Exit ideas are tested as ARMS (pipeline/arms.py), which advance_open keys per trade.
_EXIT_FIELDS = (
    "momentum_flip_exit", "reversal_momentum_flip_exit", "max_hold_bars",
    "time_stop_factor", "fill_slippage_atr", "trail_mode", "chandelier_atr_mult",
    "partial_frac", "partial_require_softening", "breakeven_after_r",
)


def _assert_shared_indicators(base: StrategyConfig, name: str, cfg: StrategyConfig) -> None:
    for field in _INDICATOR_FIELDS:
        if getattr(base, field) != getattr(cfg, field):
            raise ValueError(
                f"screen variant {name!r} changes indicator field {field!r}; variants must "
                "share the base's indicator periods (the shadow book reuses base frames)"
            )
    for field in _EXIT_FIELDS:
        if getattr(base, field) != getattr(cfg, field):
            raise ValueError(
                f"screen variant {name!r} changes exit field {field!r}, which a screen "
                "variant cannot test (open trades advance under the BASE config's exits, so "
                "the variant books an identical no-op book and mints a false null). Test "
                "exit ideas as an ARM (pipeline/arms.py) instead."
            )


def build_screen_variants(base: StrategyConfig) -> dict[str, StrategyConfig]:
    """The active screen variants, derived from ``base``.

    ``default`` is the live screen config (the only variant that carries the exit-arm
    dimension, currently baseline-only -- see pipeline/arms.py); the others are booked
    under the baseline exit. Keep this set SMALL -- each extra variant adds one more
    book to the shadow book per run. The current bake-off forward-tests the freshness
    gate threshold we ship at 2.0 ATR against a tighter 1.5.
    """
    variants = {
        DEFAULT_VARIANT: base,
        "extguard_tight": replace(base, max_extension_atr=1.5),
        # Forward shadow-track the edge-tournament's best continuation combo (volume thrust +
        # pullback-to-band + strong body). Offline it was +0.07R/cost-resilient at the full
        # universe but NOT significance-confirmed (95%low <0, thin) -- so it is measured here,
        # never surfaced/traded, to accumulate out-of-sample evidence before any promotion.
        # See docs/plans/2026-06-25-continuation-edge-tournament-design.md.
        "cont_volband": replace(base, vol_thrust_min=1.3, require_value_band=True,
                                min_trigger_body_frac=0.5, max_trigger_lower_wick_frac=1.0),
        # Forward shadow-track the edge-tournament's reversal win: requiring a HIGH-volume
        # bounce (flip volume >= 1.3x average) is cost-robust offline (+0.11R, 95%low +0.03 net
        # of 0.05 ATR slippage, n=823) -- the strongest, broadest reversal conviction filter
        # found. Measured here, not yet surfaced/traded; pending full-503 + forward confirm.
        "rev_highvol": replace(base, reversal_min_flip_rvol=1.3),
        # Roster note: 4 books/run, down from 7. The two LEGACY counterfactual books --
        # `rev_confirm1` (next-bar-only confirmation) and `rev_retrace786` (the 78.6%
        # retrace target), each kept to second-guess a 2026-07-03 flip -- were RETIRED
        # 2026-08-15. Both settled futile against the default AND, more usefully, both
        # LOST to it on the live forward book (+0.121R and +0.104R vs the default's
        # +0.190R), so neither "reverse the flip" clause fires and both flips are
        # vindicated live rather than only in replay. See edge/experiments.json.
    }
    for name, cfg in variants.items():
        if name != DEFAULT_VARIANT:
            _assert_shared_indicators(base, name, cfg)
    return variants

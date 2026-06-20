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


def _assert_shared_indicators(base: StrategyConfig, name: str, cfg: StrategyConfig) -> None:
    for field in _INDICATOR_FIELDS:
        if getattr(base, field) != getattr(cfg, field):
            raise ValueError(
                f"screen variant {name!r} changes indicator field {field!r}; variants must "
                "share the base's indicator periods (the shadow book reuses base frames)"
            )


def build_screen_variants(base: StrategyConfig) -> dict[str, StrategyConfig]:
    """The active screen variants, derived from ``base``.

    ``default`` is the live screen config (carries the full exit-arm A/B); the others
    are booked under the baseline exit only. Keep this set SMALL -- each extra variant
    adds one more book to the shadow book per run. The current bake-off forward-tests
    the freshness gate threshold we ship at 2.0 ATR against a tighter 1.5.
    """
    variants = {
        DEFAULT_VARIANT: base,
        "extguard_tight": replace(base, max_extension_atr=1.5),
    }
    for name, cfg in variants.items():
        if name != DEFAULT_VARIANT:
            _assert_shared_indicators(base, name, cfg)
    return variants

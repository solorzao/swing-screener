from collections.abc import Mapping

from swing_screener.signals.detect import PullbackContext
from swing_screener.signals.score import ScoreInputs


def _freshness(extension_atr: float, max_extension_atr: float) -> float:
    """0..1 freshness from trigger extension: 1.0 at/below EMA20, decaying to 0 at the
    anti-chase gate threshold. Falls back to a 2.0-ATR reference when the gate is off
    (max_extension_atr <= 0) so the term stays meaningful."""
    ref = max_extension_atr if max_extension_atr > 0 else 2.0
    return max(0.0, min(1.0, 1.0 - extension_atr / ref))


def build_score_inputs(ctx: PullbackContext, last_row: Mapping[str, float | bool],
                       mtf_aligned: bool, max_extension_atr: float = 2.0) -> ScoreInputs:
    """Assemble ScoreInputs from a detection context + the trigger bar's frame row.

    ``max_extension_atr`` (the anti-chase gate threshold) normalises the freshness
    term; it defaults so existing callers/tests need not supply it.
    """
    price = ctx.trigger_close
    ema_fast = float(last_row["ema_fast"])
    ema_slow = float(last_row["ema_slow"])
    return ScoreInputs(
        shaved_bottom=ctx.shaved_bottom,
        body_frac=float(last_row["body_frac"]),
        trend_slope=(ema_fast - ema_slow) / price,
        atr_pct=ctx.atr / price,
        mtf_aligned=mtf_aligned,
        rsi=ctx.rsi,
        macd_hist=float(last_row["macd_hist"]),
        macd_hist_rising=bool(last_row["macd_hist_rising"]),
        freshness=_freshness(ctx.extension_atr, max_extension_atr),
    )

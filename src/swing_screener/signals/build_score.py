from collections.abc import Mapping

from swing_screener.signals.detect import PullbackContext
from swing_screener.signals.score import ScoreInputs


def build_score_inputs(ctx: PullbackContext, last_row: Mapping[str, float | bool],
                       mtf_aligned: bool) -> ScoreInputs:
    """Assemble ScoreInputs from a detection context + the trigger bar's frame row."""
    price = ctx.trigger_close
    ema_fast = float(last_row["ema_fast"])
    ema_slow = float(last_row["ema_slow"])
    return ScoreInputs(
        shaved_bottom=ctx.shaved_bottom,
        body_frac=float(last_row["body_frac"]),
        trend_slope=(ema_fast - ema_slow) / price,
        atr_pct=ctx.atr / price,
        mtf_aligned=mtf_aligned,
    )

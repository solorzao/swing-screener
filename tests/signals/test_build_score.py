import pandas as pd

from swing_screener.signals.build_score import build_score_inputs
from swing_screener.signals.detect import PullbackContext
from swing_screener.signals.score import score_signal


def _ctx(trigger_close=100.0, atr=4.0):
    return PullbackContext(
        trigger_ts=pd.Timestamp("2024-01-10"), trigger_close=trigger_close, atr=atr,
        swing_low=96.0, pullback_bars=2, shaved_bottom=True, rsi=55.0,
    )


def test_maps_fields_from_ctx_and_row():
    ctx = _ctx()
    row = {"ema_fast": 105.0, "ema_slow": 100.0, "body_frac": 0.8}
    si = build_score_inputs(ctx, row, mtf_aligned=True)
    assert si.shaved_bottom is True
    assert si.body_frac == 0.8
    assert si.atr_pct == 4.0 / 100.0
    assert si.trend_slope == (105.0 - 100.0) / 100.0
    assert si.mtf_aligned is True


def test_result_scores_in_range():
    si = build_score_inputs(_ctx(), {"ema_fast": 105.0, "ema_slow": 100.0, "body_frac": 0.9},
                            mtf_aligned=False)
    assert 0.0 <= score_signal(si) <= 1.0

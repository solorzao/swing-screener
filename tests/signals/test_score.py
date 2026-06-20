from swing_screener.signals.score import (
    score_signal,
    ScoreInputs,
    _rsi_quality,
    _hist_accel,
)


def test_mtf_alignment_increases_score():
    base = ScoreInputs(shaved_bottom=True, body_frac=0.9, trend_slope=0.5,
                       atr_pct=0.03, mtf_aligned=False)
    aligned = ScoreInputs(shaved_bottom=True, body_frac=0.9, trend_slope=0.5,
                          atr_pct=0.03, mtf_aligned=True)
    assert score_signal(aligned) > score_signal(base)


def test_score_bounded_0_1():
    s = score_signal(ScoreInputs(True, 1.0, 1.0, 0.05, True))
    assert 0.0 <= s <= 1.0


def test_rsi_quality_bull_range():
    assert _rsi_quality(60.0) == 1.0     # at/above 50 -> full
    assert _rsi_quality(50.0) == 1.0
    assert _rsi_quality(45.0) == 0.5     # linear 40->50
    assert _rsi_quality(40.0) == 0.0
    assert _rsi_quality(35.0) == 0.0     # below floor -> 0


def test_hist_accel_momentum():
    assert _hist_accel(0.5, rising=True) == 1.0    # positive + rising
    assert _hist_accel(0.5, rising=False) == 0.5   # positive + flat/falling
    assert _hist_accel(-0.5, rising=True) == 0.0   # non-positive -> 0
    assert _hist_accel(0.0, rising=True) == 0.0


def test_strong_rsi_and_hist_outscore_weak():
    strong = ScoreInputs(shaved_bottom=True, body_frac=0.9, trend_slope=0.5,
                         atr_pct=0.03, mtf_aligned=True,
                         rsi=60.0, macd_hist=0.5, macd_hist_rising=True)
    weak = ScoreInputs(shaved_bottom=True, body_frac=0.9, trend_slope=0.5,
                       atr_pct=0.03, mtf_aligned=True,
                       rsi=35.0, macd_hist=-0.5, macd_hist_rising=False)
    assert score_signal(strong) > score_signal(weak)


def test_fresh_setup_outscores_extended_chase():
    # identical signals except freshness: the un-extended one must rank higher, so the
    # screener stops floating already-run plays to the top.
    fresh = ScoreInputs(shaved_bottom=True, body_frac=0.9, trend_slope=0.5,
                        atr_pct=0.03, mtf_aligned=True, freshness=1.0)
    chase = ScoreInputs(shaved_bottom=True, body_frac=0.9, trend_slope=0.5,
                        atr_pct=0.03, mtf_aligned=True, freshness=0.0)
    assert score_signal(fresh) > score_signal(chase)

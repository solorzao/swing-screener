from swing_screener.signals.score import score_signal, ScoreInputs


def test_mtf_alignment_increases_score():
    base = ScoreInputs(shaved_bottom=True, body_frac=0.9, trend_slope=0.5,
                       atr_pct=0.03, mtf_aligned=False)
    aligned = ScoreInputs(shaved_bottom=True, body_frac=0.9, trend_slope=0.5,
                          atr_pct=0.03, mtf_aligned=True)
    assert score_signal(aligned) > score_signal(base)


def test_score_bounded_0_1():
    s = score_signal(ScoreInputs(True, 1.0, 1.0, 0.05, True))
    assert 0.0 <= s <= 1.0

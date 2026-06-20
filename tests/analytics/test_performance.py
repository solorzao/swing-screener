from datetime import date

from swing_screener.analytics.performance import (
    breakdown,
    equity_curve,
    rank_bucket,
    score_bucket,
    summarize,
)
from swing_screener.db.models import PaperTrade


def _pt(*, fill_status="filled", status="closed", realized_r=None, hold_bars=None,
        timeframe="1d", quality_tier="reputable", rank=1, exit_date=None, score=0.8):
    return PaperTrade(
        ticker="AAPL", timeframe=timeframe, horizon="medium", signal_score=score, rank=rank,
        mtf_aligned=True, quality_tier=quality_tier, volatility_tier="med",
        fill_status=fill_status, stop=95.0, target=110.0, risk=5.0, status=status,
        realized_r=realized_r, hold_bars=hold_bars, exit_date=exit_date,
    )


def _book():
    return [
        _pt(realized_r=2.0, hold_bars=5, rank=1, exit_date=date(2024, 1, 5)),
        _pt(realized_r=-1.0, hold_bars=3, rank=2, exit_date=date(2024, 1, 3)),
        _pt(realized_r=1.0, hold_bars=4, timeframe="1wk", quality_tier="mid", rank=3,
            exit_date=date(2024, 1, 8)),
        _pt(fill_status="missed", realized_r=None, rank=4),
        _pt(fill_status="invalidated", realized_r=None, rank=5),
        _pt(status="open", realized_r=None, hold_bars=2, rank=1),  # filled but still open
    ]


def test_summarize():
    s = summarize(_book())
    assert s.n_total == 6
    assert s.n_filled == 4                       # 3 closed filled + 1 open filled
    assert s.fill_rate == 4 / 6
    assert s.n_closed == 3                        # closed + filled + realized_r set
    assert s.win_rate == 2 / 3                    # 2 of 3 closed are winners
    assert abs(s.expectancy_r - (2.0 - 1.0 + 1.0) / 3) < 1e-9
    assert s.profit_factor == 3.0                 # (2+1) / abs(-1)
    assert s.avg_hold_bars == (5 + 3 + 4) / 3


def test_summarize_empty():
    s = summarize([])
    assert s.n_total == 0 and s.fill_rate == 0.0 and s.win_rate == 0.0
    assert s.expectancy_stderr == 0.0
    assert s.expectancy_ci_low == 0.0 and s.expectancy_ci_high == 0.0


def test_expectancy_confidence_interval():
    import statistics
    s = summarize(_book())
    expected_se = statistics.stdev([2.0, -1.0, 1.0]) / (3 ** 0.5)
    assert abs(s.expectancy_stderr - expected_se) < 1e-9
    assert s.expectancy_ci_low < s.expectancy_r < s.expectancy_ci_high
    assert abs(s.expectancy_ci_low - (s.expectancy_r - 1.96 * expected_se)) < 1e-9


def test_thin_sample_interval_collapses_to_point():
    s = summarize([_pt(realized_r=1.5, hold_bars=2, exit_date=date(2024, 1, 1))])
    assert s.n_closed == 1
    assert s.expectancy_stderr == 0.0
    assert s.expectancy_ci_low == s.expectancy_r == s.expectancy_ci_high


def test_lower_bound_penalises_thin_noisy_samples():
    # identical point estimate (~1.0R), but the large tight sample earns a higher LOWER
    # bound than the tiny noisy one -- so ranking by ci_low never crowns the noise.
    tight = [_pt(realized_r=r, hold_bars=1, exit_date=date(2024, 1, 1))
             for r in (0.9, 1.1, 0.9, 1.1, 1.0, 1.0, 0.9, 1.1)]
    noisy = [_pt(realized_r=r, hold_bars=1, exit_date=date(2024, 1, 1)) for r in (-2.0, 4.0)]
    assert abs(summarize(tight).expectancy_r - summarize(noisy).expectancy_r) < 1e-9
    assert summarize(tight).expectancy_ci_low > summarize(noisy).expectancy_ci_low


def test_score_bucket_labels_and_assignment():
    trades = [
        _pt(score=0.45, realized_r=-1.0, exit_date=date(2024, 1, 1)),  # 0.00-0.50
        _pt(score=0.55, realized_r=0.0, exit_date=date(2024, 1, 2)),   # 0.50-0.60
        _pt(score=0.85, realized_r=2.0, exit_date=date(2024, 1, 3)),   # 0.80-1.00
        _pt(score=0.80, realized_r=1.0, exit_date=date(2024, 1, 4)),   # edge -> higher band
    ]
    b = score_bucket(trades, [0.5, 0.6, 0.7, 0.8])
    assert list(b) == ["0.00-0.50", "0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-1.00"]
    assert b["0.00-0.50"].n_closed == 1 and b["0.00-0.50"].expectancy_r == -1.0
    assert b["0.60-0.70"].n_closed == 0                       # empty band still present
    assert b["0.80-1.00"].n_closed == 2                       # 0.85 and the on-edge 0.80
    assert b["0.80-1.00"].expectancy_r == 1.5                 # (2.0 + 1.0) / 2


def test_score_bucket_surfaces_a_calibrated_score():
    # higher score bands earn more -> the score separates winners from losers
    trades = (
        [_pt(score=0.45, realized_r=-1.0, exit_date=date(2024, 1, 1)) for _ in range(3)]
        + [_pt(score=0.85, realized_r=2.0, exit_date=date(2024, 1, 2)) for _ in range(3)]
    )
    b = score_bucket(trades, [0.5, 0.6, 0.7, 0.8])
    assert b["0.80-1.00"].expectancy_r > b["0.00-0.50"].expectancy_r


def test_breakdown_by_timeframe():
    b = breakdown(_book(), "timeframe")
    assert set(b.keys()) == {"1d", "1wk"}
    assert b["1wk"].n_closed == 1 and b["1wk"].win_rate == 1.0


def test_rank_bucket():
    rb = rank_bucket(_book(), [2])  # buckets: "1-2", "3+"
    assert set(rb.keys()) == {"1-2", "3+"}
    assert rb["1-2"].n_total == 3   # ranks 1, 2, 1
    assert rb["3+"].n_total == 3    # ranks 3, 4, 5


def test_equity_curve_cumulative_in_exit_date_order():
    curve = equity_curve(_book())
    assert curve == [
        (date(2024, 1, 3), -1.0),
        (date(2024, 1, 5), 1.0),
        (date(2024, 1, 8), 2.0),
    ]

from datetime import date

from swing_screener.analytics.performance import (
    breakdown,
    equity_curve,
    rank_bucket,
    summarize,
)
from swing_screener.db.models import PaperTrade


def _pt(*, fill_status="filled", status="closed", realized_r=None, hold_bars=None,
        timeframe="1d", quality_tier="reputable", rank=1, exit_date=None):
    return PaperTrade(
        ticker="AAPL", timeframe=timeframe, horizon="medium", signal_score=0.8, rank=rank,
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

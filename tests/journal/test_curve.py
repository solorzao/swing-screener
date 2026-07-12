from datetime import date

from swing_screener.analytics.performance import equity_curve
from swing_screener.db.models import PaperTrade
from swing_screener.journal.curve import drawdown_series, max_drawdown


def _curve(cum_rs, start=date(2024, 1, 1)):
    """Build a (date, cum_r) curve from a cumulative-R path, one ascending day each."""
    return [(date(2024, 1, 1 + i), float(c)) for i, c in enumerate(cum_rs)]


def test_drawdown_series_golden_underwater_path():
    curve = _curve([1, 3, 2, 5, 1])
    dd = drawdown_series(curve)
    assert [r for _, r in dd] == [0.0, 0.0, 1.0, 0.0, 4.0]
    # dates are carried through unchanged, in order.
    assert [d for d, _ in dd] == [d for d, _ in curve]


def test_max_drawdown_golden():
    assert max_drawdown(_curve([1, 3, 2, 5, 1])) == 4.0


def test_drawdown_series_never_negative_and_zero_at_new_highs():
    # monotonically rising curve is never underwater.
    curve = _curve([1, 2, 3, 4])
    assert [r for _, r in drawdown_series(curve)] == [0.0, 0.0, 0.0, 0.0]
    assert max_drawdown(curve) == 0.0


def test_empty_curve():
    assert drawdown_series([]) == []
    assert max_drawdown([]) == 0.0


def _pt(*, realized_r, exit_date, account="research", fill_status="filled",
        status="closed"):
    return PaperTrade(
        ticker="AAPL", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        mtf_aligned=True, quality_tier="reputable", volatility_tier="med",
        fill_status=fill_status, stop=95.0, target=110.0, risk=5.0, status=status,
        realized_r=realized_r, exit_date=exit_date, account=account,
    )


def test_integration_over_equity_curve_filters_by_book_first():
    # research book: realized_r in exit-date order 1, 2, -1, 3, -4 -> cum 1,3,2,5,1
    research = [
        _pt(realized_r=1.0, exit_date=date(2024, 1, 1)),
        _pt(realized_r=2.0, exit_date=date(2024, 1, 2)),
        _pt(realized_r=-1.0, exit_date=date(2024, 1, 3)),
        _pt(realized_r=3.0, exit_date=date(2024, 1, 4)),
        _pt(realized_r=-4.0, exit_date=date(2024, 1, 5)),
    ]
    # a different book that would distort the curve if not filtered out first.
    paper = [_pt(realized_r=50.0, exit_date=date(2024, 1, 2), account="paper")]

    trades = research + paper
    book = [t for t in trades if t.account == "research"]
    curve = equity_curve(book)
    assert [r for _, r in curve] == [1.0, 3.0, 2.0, 5.0, 1.0]

    dd = drawdown_series(curve)
    assert [r for _, r in dd] == [0.0, 0.0, 1.0, 0.0, 4.0]
    assert max_drawdown(curve) == 4.0

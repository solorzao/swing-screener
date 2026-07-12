"""Swing discipline metrics over existing trade columns (Journal v1, Task 5)."""

from swing_screener.db.models import PaperTrade
from swing_screener.journal.discipline import discipline_report


def _pt(*, fill_status="filled", status="closed", realized_r=None, exit_reason=None,
        entry_price=None, low_water=None, high_water=None, risk=1.0):
    """A PaperTrade fake carrying the excursion + exit columns discipline reads."""
    return PaperTrade(
        ticker="AAPL", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        mtf_aligned=True, quality_tier="reputable", volatility_tier="med",
        fill_status=fill_status, stop=99.0, target=110.0, risk=risk, status=status,
        realized_r=realized_r, exit_reason=exit_reason, entry_price=entry_price,
        low_water=low_water, high_water=high_water,
    )


# The golden book: four closed-filled trades (three with excursions, one legacy
# without) plus an open and a missed trade that every metric must ignore.
def _book():
    return [
        # A: winner, exited at target, gave back part of the run.
        # mae_r=(100-98.5)/1=1.5  mfe_r=(103-100)/1=3.0  giveback=3.0-2.0=1.0
        _pt(realized_r=2.0, exit_reason="target", entry_price=100.0,
            low_water=98.5, high_water=103.0, risk=1.0),
        # B: loser, stopped out. mae_r=(100-98)/1=2.0  mfe_r=(100.5-100)/1=0.5
        # giveback=0.5-(-1.0)=1.5
        _pt(realized_r=-1.0, exit_reason="stop", entry_price=100.0,
            low_water=98.0, high_water=100.5, risk=1.0),
        # C: winner, momentum-flip exit, risk 2. mae_r=(200-199)/2=0.5
        # mfe_r=(210-200)/2=5.0  giveback=5.0-4.0=1.0
        _pt(realized_r=4.0, exit_reason="momentum_flip", entry_price=200.0,
            low_water=199.0, high_water=210.0, risk=2.0),
        # D: legacy winner, has an exit_reason but NO waters -> no excursion.
        _pt(realized_r=1.5, exit_reason="target", entry_price=100.0,
            low_water=None, high_water=None, risk=1.0),
        # ignored: still open, and never filled.
        _pt(status="open", realized_r=None),
        _pt(fill_status="missed", status="closed", realized_r=None),
    ]


def test_discipline_report_golden():
    rep = discipline_report(_book())
    # giveback = mean(1.0, 1.5, 1.0) over the three excursion trades = 3.5/3
    assert abs(rep["giveback_r"] - (3.5 / 3)) < 1e-9
    # stop share: 1 stop out of 4 closed-filled with a known exit_reason.
    assert rep["stop_honored_rate"] == 0.25
    # MAE on winners-with-excursion: mean(1.5 [A], 0.5 [C]) = 1.0 (D has no excursion).
    assert rep["avg_mae_before_win"] == 1.0
    assert rep["n_closed"] == 4
    assert rep["n_with_excursion"] == 3
    assert rep["n_wins"] == 2
    assert rep["n_stopped"] == 1
    assert rep["n_with_exit_reason"] == 4


def test_discipline_report_empty_is_none_safe():
    rep = discipline_report([])
    assert rep["giveback_r"] is None
    assert rep["stop_honored_rate"] is None
    assert rep["avg_mae_before_win"] is None
    assert rep["n_closed"] == 0
    assert rep["n_with_excursion"] == 0
    assert rep["n_wins"] == 0
    assert rep["n_stopped"] == 0
    assert rep["n_with_exit_reason"] == 0


def test_only_open_and_missed_trades_yield_none_metrics():
    trades = [
        _pt(status="open", realized_r=None),
        _pt(fill_status="missed", status="closed", realized_r=None),
    ]
    rep = discipline_report(trades)
    assert rep["n_closed"] == 0
    assert rep["giveback_r"] is None
    assert rep["stop_honored_rate"] is None
    assert rep["avg_mae_before_win"] is None


def test_closed_trade_without_exit_reason_excluded_from_stop_rate():
    # A closed-filled trade whose exit_reason is None cannot prove how it exited, so it
    # counts toward n_closed but neither the numerator nor the denominator of the rate.
    trades = [
        _pt(realized_r=-1.0, exit_reason="stop", entry_price=100.0,
            low_water=98.0, high_water=100.5, risk=1.0),
        _pt(realized_r=1.0, exit_reason=None, entry_price=100.0,
            low_water=99.0, high_water=102.0, risk=1.0),
    ]
    rep = discipline_report(trades)
    assert rep["n_closed"] == 2
    assert rep["n_with_exit_reason"] == 1
    assert rep["n_stopped"] == 1
    assert rep["stop_honored_rate"] == 1.0


def test_risk_zero_and_missing_waters_skip_excursion_but_keep_reason():
    # risk 0 -> no excursion (guarded like Task 1); still a valid stop for the rate.
    trades = [
        _pt(realized_r=-1.0, exit_reason="stop", entry_price=100.0,
            low_water=98.0, high_water=100.5, risk=0.0),
    ]
    rep = discipline_report(trades)
    assert rep["n_closed"] == 1
    assert rep["n_with_excursion"] == 0
    assert rep["giveback_r"] is None
    assert rep["avg_mae_before_win"] is None
    assert rep["stop_honored_rate"] == 1.0
    assert rep["n_stopped"] == 1

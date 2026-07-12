"""Tests for MAE/MFE excursions in R (journal.excursions)."""

from __future__ import annotations

from swing_screener.db.models import PaperTrade
from swing_screener.journal.excursions import Excursion, excursion_r, excursion_summary


def _trade(
    *,
    entry_price: float | None = 100.0,
    low_water: float | None = 98.5,
    high_water: float | None = 103.0,
    risk: float | None = 1.0,
    status: str = "closed",
    fill_status: str = "filled",
    realized_r: float | None = 2.0,
) -> PaperTrade:
    """A closed-filled paper trade stand-in built directly (no session/flush)."""
    return PaperTrade(
        entry_price=entry_price,
        low_water=low_water,
        high_water=high_water,
        risk=risk,
        status=status,
        fill_status=fill_status,
        realized_r=realized_r,
    )


def test_excursion_r_golden() -> None:
    # entry 100, stop 99 (risk 1), low_water 98.5, high_water 103.
    exc = excursion_r(_trade())
    assert exc == Excursion(mae_r=1.5, mfe_r=3.0)


def test_excursion_r_none_when_low_water_missing() -> None:
    assert excursion_r(_trade(low_water=None)) is None


def test_excursion_r_none_when_high_water_missing() -> None:
    assert excursion_r(_trade(high_water=None)) is None


def test_excursion_r_none_when_entry_price_missing() -> None:
    assert excursion_r(_trade(entry_price=None)) is None


def test_excursion_r_none_when_risk_missing() -> None:
    assert excursion_r(_trade(risk=None)) is None


def test_excursion_r_none_when_risk_zero() -> None:
    assert excursion_r(_trade(risk=0.0)) is None


def test_excursion_summary_empty_is_zeros() -> None:
    assert excursion_summary([]) == {
        "n": 0,
        "avg_mae_r": 0.0,
        "avg_mfe_r": 0.0,
        "median_mae_r": 0.0,
        "median_mfe_r": 0.0,
    }


def test_excursion_summary_zeros_when_no_non_none_excursions() -> None:
    # Closed-filled but missing excursion instrumentation -> excluded -> zeros.
    assert excursion_summary([_trade(low_water=None)]) == {
        "n": 0,
        "avg_mae_r": 0.0,
        "avg_mfe_r": 0.0,
        "median_mae_r": 0.0,
        "median_mfe_r": 0.0,
    }


def test_excursion_summary_skips_open_and_unfilled() -> None:
    trades = [
        _trade(),  # counted
        _trade(status="open"),  # excluded: not closed
        _trade(fill_status="missed"),  # excluded: not filled
        _trade(realized_r=None),  # excluded: no realized R
    ]
    summary = excursion_summary(trades)
    assert summary["n"] == 1


def test_excursion_summary_aggregates() -> None:
    # Two trades: mae_r {1.5, 2.5} -> avg 2.0, median 2.0; mfe_r {3.0, 5.0} -> avg 4.0.
    trades = [
        _trade(entry_price=100.0, low_water=98.5, high_water=103.0, risk=1.0),
        _trade(entry_price=100.0, low_water=97.5, high_water=105.0, risk=1.0),
    ]
    summary = excursion_summary(trades)
    assert summary["n"] == 2
    assert summary["avg_mae_r"] == 2.0
    assert summary["avg_mfe_r"] == 4.0
    assert summary["median_mae_r"] == 2.0
    assert summary["median_mfe_r"] == 4.0

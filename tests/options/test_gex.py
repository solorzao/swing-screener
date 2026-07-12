from datetime import date

import pandas as pd

from swing_screener.options.config import GexConfig
from swing_screener.options.gex import bs_gamma, compute_gex


def _chain(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


_EXP = date(2026, 7, 17)
_ASOF = date(2026, 7, 13)


def test_bs_gamma_peaks_at_the_money() -> None:
    atm = bs_gamma(spot=100, strike=100, iv=0.2, t_years=0.02)
    otm = bs_gamma(spot=100, strike=120, iv=0.2, t_years=0.02)
    assert atm > otm > 0


def test_bs_gamma_degenerate_inputs_are_zero() -> None:
    assert bs_gamma(100, 100, iv=0.0, t_years=0.02) == 0.0
    assert bs_gamma(100, 100, iv=0.2, t_years=0.0) == 0.0


def test_walls_flip_and_regime() -> None:
    # Calls above spot share OI, so gamma (closer strike = higher gamma) sets the
    # wall at 102; puts below spot likewise set it at 98. Calls carry more OI than
    # puts, so cumulative signed gamma crosses zero between the put and call sides.
    rows = [
        {"expiry": _EXP, "strike": 102.0, "right": "C", "open_interest": 40_000, "iv": 0.20},
        {"expiry": _EXP, "strike": 105.0, "right": "C", "open_interest": 40_000, "iv": 0.20},
        {"expiry": _EXP, "strike": 98.0, "right": "P", "open_interest": 20_000, "iv": 0.20},
        {"expiry": _EXP, "strike": 95.0, "right": "P", "open_interest": 20_000, "iv": 0.20},
    ]
    levels = compute_gex(_chain(rows), spot=100.0, asof=_ASOF, cfg=GexConfig())
    assert levels.call_wall == 102.0
    assert levels.put_wall == 98.0
    assert levels.gamma_flip is not None and 95.0 < levels.gamma_flip < 105.0
    assert levels.regime in {"positive", "negative"}
    assert levels.net_gex != 0.0


def test_gamma_weighting_beats_raw_oi() -> None:
    # The nearer call has a QUARTER of the far strike's open interest, but far more
    # gamma -- so it is the wall. Pins the model as gamma-weighted, not OI concentration.
    rows = [
        {"expiry": _EXP, "strike": 101.0, "right": "C", "open_interest": 10_000, "iv": 0.20},
        {"expiry": _EXP, "strike": 112.0, "right": "C", "open_interest": 40_000, "iv": 0.20},
        {"expiry": _EXP, "strike": 99.0, "right": "P", "open_interest": 10_000, "iv": 0.20},
    ]
    levels = compute_gex(_chain(rows), spot=100.0, asof=_ASOF, cfg=GexConfig())
    assert levels.call_wall == 101.0


def test_empty_chain_yields_unknowns() -> None:
    levels = compute_gex(_chain([]), spot=100.0, asof=_ASOF, cfg=GexConfig())
    assert levels.call_wall is None and levels.put_wall is None
    assert levels.gamma_flip is None
    assert levels.regime == "unknown"

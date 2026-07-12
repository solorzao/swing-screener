"""Naive dealer-gamma (GEX) model: Black-Scholes gamma x open interest.

Convention (the standard public-dashboard model, and Nick Ireland's definitions):
per-strike dollar GEX = gamma * OI * 100 * spot^2 * 0.01, calls positive and puts
negative. The call wall is the strike with the largest CALL GAMMA above spot, the
put wall the largest PUT GAMMA below spot, and the gamma flip is where cumulative
signed gamma crosses zero -- all GAMMA-weighted, not raw open interest. This
ignores actual dealer positioning -- documented assumption, see
docs/modules/gex-lab.md. Levels are for structure, not prophecy.

Gamma-weighting (not OI concentration) is deliberate: it is the definition of the
strategy's walls, and on real chains a far strike with big OI but little gamma does
not pin price the way a nearer, higher-gamma strike does.
"""

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from swing_screener.options.config import GexConfig


def bs_gamma(spot: float, strike: float, iv: float, t_years: float,
             r: float = 0.04) -> float:
    """Black-Scholes gamma. No scipy: gamma needs only the normal PDF."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + iv * iv / 2.0) * t_years) / (iv * math.sqrt(t_years))
    pdf = math.exp(-d1 * d1 / 2.0) / math.sqrt(2.0 * math.pi)
    return pdf / (spot * iv * math.sqrt(t_years))


@dataclass(frozen=True)
class StrikeGamma:
    strike: float
    call_gex: float   # dollar gamma from calls at this strike (>= 0)
    put_gex: float    # dollar gamma from puts at this strike (<= 0)

    @property
    def net(self) -> float:
        return self.call_gex + self.put_gex


@dataclass(frozen=True)
class GexLevels:
    spot: float
    call_wall: float | None
    put_wall: float | None
    gamma_flip: float | None
    net_gex: float
    regime: str  # positive | negative | unknown
    profile: tuple[StrikeGamma, ...] = field(default=())


def _unknowns(spot: float) -> GexLevels:
    return GexLevels(spot=spot, call_wall=None, put_wall=None,
                     gamma_flip=None, net_gex=0.0, regime="unknown")


def compute_gex(chain: pd.DataFrame, spot: float, asof: date, cfg: GexConfig) -> GexLevels:
    if chain.empty:
        return _unknowns(spot)
    df = chain.copy()
    lo, hi = spot * (1 - cfg.strike_window_pct), spot * (1 + cfg.strike_window_pct)
    df = df[(df["strike"] >= lo) & (df["strike"] <= hi)]
    if df.empty:
        return _unknowns(spot)

    def _t(expiry: date) -> float:
        # Calendar-day fraction of a year; floor at half a day so 0DTE still
        # carries gamma instead of dividing by zero.
        days = max((expiry - asof).days, 0.5)
        return days / 365.0

    df["gamma"] = [
        bs_gamma(spot, row.strike, row.iv, _t(row.expiry), cfg.risk_free_rate)
        for row in df.itertuples()
    ]
    # Unsigned dollar gamma per 1% move; the call/put sign is applied per side below.
    df["gex"] = df["gamma"] * df["open_interest"] * 100.0 * spot * spot * 0.01

    # Two-groupby split (the plan-sanctioned fallback for the typed-lambda groupby):
    # per-strike dollar gamma on each side. Calls contribute +GEX, puts -GEX.
    call_gex_by = df[df["right"] == "C"].groupby("strike")["gex"].sum()
    put_gex_by = df[df["right"] == "P"].groupby("strike")["gex"].sum()

    strikes = sorted(set(call_gex_by.index) | set(put_gex_by.index))
    profile = tuple(
        StrikeGamma(
            strike=float(k),
            call_gex=float(call_gex_by.get(k, 0.0)),
            put_gex=-float(put_gex_by.get(k, 0.0)),
        )
        for k in strikes
    )

    # Walls = the largest GAMMA concentration on each side (calls at/above spot,
    # puts at/below spot). The definition of the strategy's walls.
    calls_above = [p for p in profile if p.call_gex > 0 and p.strike >= spot]
    puts_below = [p for p in profile if p.put_gex < 0 and p.strike <= spot]
    call_wall = max(calls_above, key=lambda p: p.call_gex).strike if calls_above else None
    put_wall = min(puts_below, key=lambda p: p.put_gex).strike if puts_below else None

    # Gamma flip: strike where cumulative signed dollar gamma (ascending strikes)
    # crosses from negative to positive, linearly interpolated between the brackets.
    flip: float | None = None
    cum = 0.0
    prev_strike: float | None = None
    prev_cum: float | None = None
    for p in profile:
        cum += p.net
        if prev_cum is not None and prev_cum < 0 <= cum and prev_strike is not None:
            frac = -prev_cum / (cum - prev_cum) if cum != prev_cum else 0.0
            flip = prev_strike + frac * (p.strike - prev_strike)
            break
        prev_strike, prev_cum = p.strike, cum

    net = float(sum(p.net for p in profile))
    if flip is not None:
        regime = "positive" if spot >= flip else "negative"
    else:
        regime = "positive" if net > 0 else "negative" if net < 0 else "unknown"
    return GexLevels(spot=spot, call_wall=call_wall, put_wall=put_wall,
                     gamma_flip=flip, net_gex=net, regime=regime, profile=profile)

"""Morning day-plan matrix + GEX-snapshot persistence.

The plan is a small decision matrix over two facts: the daily EMA-stack bias
(directional or tangled) and the dealer-gamma regime (positive/negative/unknown).
It never sizes or triggers a trade -- it only says whether the day is a breakout
day, a range day, or a stand-down (see docs/modules/gex-lab.md).

Matrix:
- tangled daily              -> stand_down (no directional edge)
- directional + negative GEX -> breakout  (dealers amplify moves; ride the trend)
- directional + positive GEX -> range     (dealers pin; fade toward the walls)
- regime unknown             -> stand_down (no map, no trade)
"""

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
from sqlalchemy.orm import Session

from swing_screener.db.models import GexSnapshot
from swing_screener.options.bias import stack_state
from swing_screener.options.config import GexConfig
from swing_screener.options.gex import GexLevels


@dataclass(frozen=True)
class DayPlan:
    underlying: str
    bias: str          # bullish | bearish | tangled (from the EMA stack)
    regime: str        # positive | negative | unknown (from GEX)
    call: str          # breakout | range | stand_down
    levels: GexLevels
    spacing_pct: float


def build_plan(underlying: str, daily_close: pd.Series, levels: GexLevels,
               cfg: GexConfig) -> DayPlan:
    """Classify today's session into a breakout / range / stand-down call."""
    stack = stack_state(daily_close, cfg)
    if stack.direction == "tangled":
        call = "stand_down"
    elif levels.regime == "negative":
        call = "breakout"
    elif levels.regime == "positive":
        call = "range"
    else:  # regime unknown -- no map, no trade
        call = "stand_down"
    return DayPlan(
        underlying=underlying,
        bias=stack.direction,
        regime=levels.regime,
        call=call,
        levels=levels,
        spacing_pct=stack.spacing_pct,
    )


def save_snapshot(session: Session, *, underlying: str, ts: datetime,
                  levels: GexLevels, thin: bool,
                  source: str = "computed") -> GexSnapshot:
    """Persist a computed GEX map; the per-strike profile serializes to JSON."""
    profile_json = json.dumps([dataclasses.asdict(p) for p in levels.profile])
    snap = GexSnapshot(
        underlying=underlying,
        ts=ts,
        spot=levels.spot,
        call_wall=levels.call_wall,
        put_wall=levels.put_wall,
        gamma_flip=levels.gamma_flip,
        net_gex=levels.net_gex,
        regime=levels.regime,
        profile_json=profile_json,
        thin_chain=thin,
        source=source,
    )
    session.add(snap)
    session.commit()
    session.refresh(snap)
    return snap

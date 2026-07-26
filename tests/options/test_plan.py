from datetime import UTC, datetime

import pandas as pd
from sqlalchemy.orm import Session

from swing_screener.db.models import GexSnapshot
from swing_screener.db.session import get_engine
from swing_screener.options.config import GexConfig
from swing_screener.options.gex import GexLevels
from swing_screener.options.plan import build_plan, save_snapshot


def _levels(regime: str) -> GexLevels:
    return GexLevels(spot=100.0, call_wall=105.0, put_wall=95.0, gamma_flip=99.0,
                     net_gex=1e9, regime=regime)


def _daily_up() -> pd.Series:
    return pd.Series([100 + 0.5 * i for i in range(120)])


def test_negative_gamma_plus_trend_is_breakout_day() -> None:
    plan = build_plan("SPY", _daily_up(), _levels("negative"), GexConfig())
    assert plan.bias == "bullish"
    assert plan.call == "breakout"


def test_positive_gamma_plus_trend_is_range_day() -> None:
    assert build_plan("SPY", _daily_up(), _levels("positive"), GexConfig()).call == "range"


def test_tangled_daily_is_stand_down() -> None:
    chop = pd.Series([100 + (1 if i % 2 else -1) for i in range(120)])
    assert build_plan("SPY", chop, _levels("negative"), GexConfig()).call == "stand_down"


def test_save_snapshot_persists_levels_and_profile() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = save_snapshot(s, underlying="SPY", ts=datetime(2026, 7, 13, 9, 10, tzinfo=UTC),
                            levels=_levels("positive"), thin=False)
        assert row.id is not None
        got = s.get(GexSnapshot, row.id)
        assert got is not None and got.regime == "positive" and got.call_wall == 105.0

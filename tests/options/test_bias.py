import pandas as pd

from swing_screener.options.bias import stack_state
from swing_screener.options.config import GexConfig


def _trend(start: float, step: float, n: int = 120) -> pd.Series:
    return pd.Series([start + step * i for i in range(n)])


def test_uptrend_is_bullish() -> None:
    s = stack_state(_trend(100, 0.5), cfg=GexConfig())
    assert s.direction == "bullish"
    assert s.spacing_pct > 0


def test_downtrend_is_bearish() -> None:
    assert stack_state(_trend(200, -0.5), cfg=GexConfig()).direction == "bearish"


def test_chop_is_tangled() -> None:
    chop = pd.Series([100 + (1 if i % 2 else -1) for i in range(120)])
    assert stack_state(chop, cfg=GexConfig()).direction == "tangled"


def test_too_short_series_is_tangled() -> None:
    assert stack_state(pd.Series([1.0, 2.0]), cfg=GexConfig()).direction == "tangled"

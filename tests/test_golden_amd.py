"""Golden test: the engine must reproduce the user's AMD daily 2018 setup.

Data is frozen in tests/fixtures/amd_daily_2018.csv (see scripts/make_amd_fixture.py).
The strategy must flag a long trigger inside the mid-June..mid-July pullback zone and
must NOT fire during the preceding April..May uptrend run (no preceding pullback).
"""
from pathlib import Path

import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.signals.detect import detect_last_bar
from swing_screener.signals.frame import build_frame

# anchor to this file, not the process cwd, so the test runs from anywhere
FIXTURE = Path(__file__).parent / "fixtures" / "amd_daily_2018.csv"


def _load() -> pd.DataFrame:
    df = pd.read_csv(FIXTURE, index_col=0, parse_dates=True)
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def _signal_dates(df: pd.DataFrame, cfg: StrategyConfig) -> list[pd.Timestamp]:
    """Sliding evaluation: a signal 'fires' on date D if detect_last_bar is truthy
    when the frame ends at D."""
    full = build_frame(df, cfg)
    dates: list[pd.Timestamp] = []
    # start past the warmup (EMA-slow span + classification/pullback lookback)
    for i in range(cfg.ema_slow + 6, len(full)):
        if detect_last_bar(full.iloc[: i + 1], cfg) is not None:
            dates.append(full.index[i])
    return dates


def test_amd_fires_in_pullback_zone_not_in_uptrend():
    cfg = StrategyConfig()
    fired = _signal_dates(_load(), cfg)

    zone = [d for d in fired
            if pd.Timestamp("2018-06-08") <= d <= pd.Timestamp("2018-07-20")]
    uptrend_run = [d for d in fired
                   if pd.Timestamp("2018-04-10") <= d <= pd.Timestamp("2018-05-20")]

    assert zone, "engine should flag a long trigger inside the AMD pullback zone"
    assert not uptrend_run, "engine should not fire mid-uptrend with no preceding pullback"

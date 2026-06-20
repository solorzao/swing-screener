from pathlib import Path

import pandas as pd

from swing_screener.analytics.performance import summarize
from swing_screener.config import StrategyConfig
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.optimize import (
    OptimizeResult,
    build_config_grid,
    format_report,
    optimize,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "amd_daily_2018.csv"


def _amd() -> pd.DataFrame:
    df = pd.read_csv(FIXTURE, index_col=0, parse_dates=True)
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def _summary(rs):
    trades = [PaperTrade(ticker="X", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
                         fill_status="filled", stop=95.0, target=110.0, risk=5.0,
                         status="closed", realized_r=r, hold_bars=3) for r in rs]
    return summarize(trades)


def test_config_grid_sweeps_the_freshness_gate_including_the_incumbent():
    base = StrategyConfig(max_extension_atr=2.0)
    grid = build_config_grid(base)
    exts = sorted(c.max_extension_atr for c in grid.values())
    assert exts == [1.0, 1.5, 2.0, 2.5]            # sweep
    assert "ext_2.0" in grid                        # the shipped default is in the bake-off
    # every grid point keeps the base's indicator periods (frames are reused)
    assert all(c.ema_fast == base.ema_fast and c.atr_period == base.atr_period
               for c in grid.values())


def test_optimize_returns_walk_forward_result_over_amd():
    # AMD's fresh setups sit late in this short fixture; a small OOS slice keeps them
    # in-sample so a winner is chosen. replay only reports configs that actually traded.
    result = optimize({"AMD": _amd()}, timeframe="1d", oos_frac=0.1)
    assert isinstance(result, OptimizeResult)
    grid_names = set(build_config_grid(StrategyConfig()))
    assert set(result.in_sample) <= grid_names and set(result.in_sample)   # some traded
    assert set(result.out_of_sample) <= grid_names
    assert result.winner in grid_names
    assert result.in_sample[result.winner].n_closed > 0


def test_format_report_surfaces_winner_and_oos_line():
    report = format_report(optimize({"AMD": _amd()}, timeframe="1d", oos_frac=0.1))
    assert "IN-SAMPLE leaderboard" in report
    assert "Winner (in-sample):" in report
    assert "out-of-sample" in report


def test_optimize_with_no_trades_proposes_nothing():
    flat = pd.DataFrame(
        {"open": 50.0, "high": 50.1, "low": 49.9, "close": 50.0, "volume": 1e6},
        index=pd.date_range("2020-01-01", periods=260, freq="D"),
    )
    result = optimize({"FLAT": flat}, timeframe="1d")
    assert result.winner is None
    assert "nothing to propose" in format_report(result)


def test_report_verdict_promotes_when_edge_holds_out_of_sample():
    result = OptimizeResult(
        in_sample={"ext_1.5": _summary([1.0, 1.0, 1.0])},
        out_of_sample={"ext_1.5": _summary([1.0] * 6)},   # tight, positive lower bound
        winner="ext_1.5",
    )
    assert "consider promoting" in format_report(result)


def test_report_verdict_keeps_current_when_edge_does_not_hold():
    result = OptimizeResult(
        in_sample={"ext_1.5": _summary([1.0, 1.0, 1.0])},
        out_of_sample={"ext_1.5": _summary([2.0, -2.0])},  # mean ~0, lower bound < 0
        winner="ext_1.5",
    )
    assert "keep current config" in format_report(result)

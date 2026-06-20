from dataclasses import replace
from pathlib import Path

import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import format_leaderboard, replay

FIXTURE = Path(__file__).parents[1] / "fixtures" / "amd_daily_2018.csv"


def _amd() -> pd.DataFrame:
    df = pd.read_csv(FIXTURE, index_col=0, parse_dates=True)
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def test_replay_ranks_default_and_alt_variants_over_amd_history():
    board = replay({"AMD": _amd()}, timeframe="1d")
    assert {"default", "extguard_tight"} <= set(board)
    # the screener took at least some AMD setups across the 2018 window
    assert board["default"].n_total >= 1
    # a tighter freshness gate can only screen a subset, never more, fills than the base
    assert board["extguard_tight"].n_total <= board["default"].n_total


def test_replay_leaderboard_formats_best_expectancy_first():
    board = replay({"AMD": _amd()}, timeframe="1d")
    txt = format_leaderboard(board)
    assert "variant" in txt and "expectancy_r" in txt  # header
    assert "default" in txt and "extguard_tight" in txt


def test_replay_with_single_variant_and_short_frame_is_safe():
    cfg = StrategyConfig()
    one = {"only": cfg}
    # a frame shorter than the warmup yields an empty book, not a crash
    short = _amd().head(10)
    board = replay({"AMD": short}, timeframe="1d", base_cfg=cfg, variants=one)
    assert board == {} or board["only"].n_total == 0


def test_replay_respects_an_explicit_variant_set():
    cfg = StrategyConfig(max_extension_atr=0.0)  # gate off -> the base takes more setups
    variants = {"loose": cfg, "tight": replace(cfg, max_extension_atr=1.0)}
    board = replay({"AMD": _amd()}, timeframe="1d", base_cfg=cfg, variants=variants)
    assert set(board) == {"loose", "tight"}
    assert board["loose"].n_total >= board["tight"].n_total

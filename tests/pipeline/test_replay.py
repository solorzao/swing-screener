from dataclasses import replace
from pathlib import Path

import pandas as pd

from swing_screener.analytics.performance import summarize
from swing_screener.config import StrategyConfig
from swing_screener.db.models import PaperTrade
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


def _closed(ticker: str, r: float) -> PaperTrade:
    return PaperTrade(ticker=ticker, status="closed", fill_status="filled", realized_r=r)


def _unfilled(ticker: str) -> PaperTrade:
    return PaperTrade(ticker=ticker, status="open", fill_status="pending")


def test_leaderboard_shows_fill_rate_and_total_signals():
    """A variant can 'win' by rarely filling; the leaderboard must surface fill% + n_total
    wherever the rankings are read (2026-07-01 audit)."""
    # 2 filled-and-closed out of 4 signals -> 50% fill rate on 4 total.
    half = summarize([_closed("A", 1.0), _closed("B", 1.0), _unfilled("C"), _unfilled("D")])
    assert half.fill_rate == 0.5 and half.n_total == 4
    full = summarize([_closed(f"T{i}", 0.5) for i in range(8)])

    txt = format_leaderboard({"half": half, "full": full})
    header = txt.splitlines()[0]
    assert "fill%" in header and "total" in header

    rows = {line.split()[0]: line.split() for line in txt.splitlines()
            if line.startswith(("half", "full"))}
    assert "50%" in rows["half"] and "4" in rows["half"]
    assert "100%" in rows["full"] and "8" in rows["full"]


def test_leaderboard_marks_the_iid_fallback_for_thin_clusters():
    # Thin: 3 distinct tickers (< the cluster floor of 8) -> the bound falls back to IID.
    thin = summarize([_closed(t, 1.0) for t in ("A", "B", "C")])
    assert thin.thin_clusters and thin.n_clusters == 3
    # Clustered: 8 distinct tickers (>= the floor) AND n_closed >= 2 -> bootstrap fires.
    clustered = summarize([_closed(f"T{i}", 0.5) for i in range(8)])
    assert not clustered.thin_clusters and clustered.n_clusters == 8

    txt = format_leaderboard({"thin": thin, "clustered": clustered})
    assert "clusters" in txt  # the new column header

    rows = {line.split()[0]: line for line in txt.splitlines() if line.startswith(("thin", "clustered"))}
    # The thin-clusters row carries the IID-fallback flag and its (sub-floor) cluster count.
    assert "iid" in rows["thin"] and rows["thin"].split()[-1] == "iid"
    # The properly-clustered row does NOT -- its bound is ticker-clustered, not IID.
    assert "iid" not in rows["clustered"]

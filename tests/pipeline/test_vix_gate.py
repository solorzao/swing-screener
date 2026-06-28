"""VIX-percentile-rank regime overlay for the reversal book (edge-discovery exp 15).

Covers the pure rank/bucket functions, the point-in-time by-date map (no lookahead), the
mockable ^VIX fetch (no network), and the replay gate that suppresses reversal fills in
high-VIX states while stamping vix_bucket for breakdown().
"""

from datetime import date

import pandas as pd
import pytest

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.regime import vix_bucket, vix_percentile_rank
from swing_screener.pipeline.replay import _vix_rank_by_date, replay_book


def _series(values, start="2023-01-01"):
    return pd.Series([float(v) for v in values],
                     index=pd.date_range(start, periods=len(values), freq="D"))


# --- pure rank + bucket ------------------------------------------------------

def test_vix_percentile_rank_is_fraction_below_current():
    # current=30; 2 of the 3 trailing values are below it -> 66.7
    assert vix_percentile_rank(_series([10, 20, 30])) == pytest.approx(2 / 3 * 100)
    # current is the max of 10 -> 9/10 below
    assert vix_percentile_rank(_series(range(10, 110, 10))) == pytest.approx(90.0)


def test_vix_rank_uses_only_trailing_252():
    # the old 9999 sits OUTSIDE the trailing-252 window, so it must not affect today's rank
    tail252 = list(range(10, 262))           # 252 values, last = 261
    s = _series([9999] + tail252)            # 253 values; 9999 is the 253rd-back, excluded
    assert vix_percentile_rank(s) == pytest.approx(251 / 252 * 100)


def test_vix_bucket_thresholds():
    assert vix_bucket(10.0) == "low"      # < 40
    assert vix_bucket(39.9) == "low"
    assert vix_bucket(40.0) == "mid"      # 40-70 inclusive
    assert vix_bucket(70.0) == "mid"
    assert vix_bucket(70.1) == "high"     # > 70
    assert vix_bucket(None) is None


# --- point-in-time by-date map (no lookahead) --------------------------------

def test_vix_rank_by_date_is_point_in_time():
    vix = pd.DataFrame({"close": [10.0, 10.0, 10.0, 100.0, 10.0]},
                       index=pd.date_range("2024-01-01", periods=5, freq="D"))
    ranks = _vix_rank_by_date(vix)
    # day with the 100 spike ranks high as-of that day (3 of 4 trailing below it)
    assert ranks[date(2024, 1, 4)] == pytest.approx(75.0)
    # the next day (back to 10) ranks low -- the spike is in the past, not lookahead
    assert ranks[date(2024, 1, 5)] == pytest.approx(0.0)
    # the first day has nothing below it
    assert ranks[date(2024, 1, 1)] == pytest.approx(0.0)


# --- mockable ^VIX fetch (no network) ----------------------------------------

def test_fetch_vix_uses_download_seam_no_network(tmp_path, monkeypatch):
    from swing_screener.data import fetch as fetch_mod

    calls = {}

    def fake_download(ticker, interval, period):
        calls["ticker"] = ticker
        idx = pd.date_range("2024-01-01", periods=3, freq="D")
        return pd.DataFrame({"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
                             "close": [11.0, 12.0, 13.0], "volume": [0, 0, 0]}, index=idx)

    monkeypatch.setattr(fetch_mod, "_download", fake_download)
    out = fetch_mod.fetch_vix(cache_dir=tmp_path)
    assert calls["ticker"] == "^VIX"
    assert out is not None and list(out["close"]) == [11.0, 12.0, 13.0]


# --- replay gate + stamp -----------------------------------------------------

def _reversal_raw():
    """Flat base, steep decline below the slow EMA, then a heavy-volume green flip --
    produces at least one reversal signal over the replay walk."""
    rows, p = [], 100.0
    for i in range(46):
        o = p
        c = p + (0.5 if i % 2 else -0.5)
        rows.append({"open": o, "high": max(o, c) + 0.3, "low": min(o, c) - 0.3, "close": c, "volume": 1e6})
        p = c
    for _ in range(14):
        o = p
        c = p - 2.2
        rows.append({"open": o, "high": o + 0.2, "low": c - 0.3, "close": c, "volume": 1e6})
        p = c
    o = p + 0.2
    c = p + 12.0
    rows.append({"open": o, "high": c + 0.5, "low": p - 0.1, "close": c, "volume": 3e6})
    rows.append({"open": c, "high": c + 3.3, "low": c - 0.3, "close": c + 2.5, "volume": 2.5e6})
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx)


def _vix_over(index, rising: bool):
    vals = list(range(10, 10 + len(index))) if rising else list(range(10 + len(index), 10, -1))
    return pd.DataFrame({"close": [float(v) for v in vals]}, index=index)


def _reversal_trades(trades):
    return [t for t in trades if t.play_type == "reversal"]


def test_vix_gate_suppresses_reversal_fills_in_high_vix():
    raw = _reversal_raw()
    frames = {"AAA": raw}
    base = StrategyConfig()
    from dataclasses import replace
    gated = replace(base, max_vix_rank=70.0)

    # rising VIX -> late (fill) dates rank high -> gate suppresses reversal fills
    hi = replay_book(frames, timeframe="1d", base_cfg=base, variants={"v": gated},
                     vix_daily=_vix_over(raw.index, rising=True))
    # falling VIX -> late dates rank low -> reversal fills allowed
    lo = replay_book(frames, timeframe="1d", base_cfg=base, variants={"v": gated},
                     vix_daily=_vix_over(raw.index, rising=False))

    assert len(_reversal_trades(lo)) > 0
    assert len(_reversal_trades(hi)) == 0


def test_vix_bucket_is_stamped_on_fills():
    raw = _reversal_raw()
    base = StrategyConfig()
    trades = replay_book({"AAA": raw}, timeframe="1d", base_cfg=base,
                         variants={"default": base}, vix_daily=_vix_over(raw.index, rising=False))
    rev = _reversal_trades(trades)
    assert rev and all(t.vix_bucket in {"low", "mid", "high"} for t in rev)

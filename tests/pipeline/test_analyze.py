from swing_screener.config import StrategyConfig
from swing_screener.pipeline.analyze import analyze_ticker


def _firing(bars):
    # uptrend -> shaved-head pullback -> decisive green trigger (HA flips bullish)
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})
    return bars(rows)


def _uptrend(bars):
    return bars([{"open": 10 + i, "high": 11 + i, "low": 9 + i, "close": 10.5 + i}
                 for i in range(60)])


def test_fires_with_zone_score_and_tags(bars):
    res = analyze_ticker("AAPL", {"1d": _firing(bars)}, StrategyConfig())
    assert len(res) == 1
    r = res[0]
    assert r.ticker == "AAPL" and r.timeframe == "1d" and r.horizon == "medium"
    assert r.entry_floor < r.entry_ceiling and r.stop < r.entry_floor
    assert 0.0 <= r.score <= 1.0
    assert r.mtf_aligned is False           # no higher tf supplied
    assert r.quality_tier in {"penny", "speculative", "mid", "reputable"}
    assert r.volatility_tier in {"low", "med", "high"}


def test_mtf_alignment_boosts_score(bars):
    base = analyze_ticker("AAPL", {"1d": _firing(bars)}, StrategyConfig())[0]
    aligned = analyze_ticker(
        "AAPL", {"1d": _firing(bars), "1wk": _uptrend(bars)}, StrategyConfig()
    )[0]
    assert aligned.mtf_aligned is True
    assert aligned.score > base.score


def test_no_fire_returns_empty(bars):
    res = analyze_ticker("AAPL", {"1d": _uptrend(bars)}, StrategyConfig())
    assert res == []

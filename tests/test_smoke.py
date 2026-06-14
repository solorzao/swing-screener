"""Smoke test so CI has something to run before the engine lands (Task 1+)."""
import swing_screener


def test_package_imports():
    assert swing_screener.__version__ == "0.1.0"


def test_bars_fixture_builds(bars):
    df = bars([{"open": 10, "high": 12, "low": 9, "close": 11}])
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 1

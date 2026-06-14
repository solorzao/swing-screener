import pandas as pd
import pytest


def make_bars(rows: list[dict], start: str = "2024-01-01", freq: str = "D") -> pd.DataFrame:
    """Build an OHLCV DataFrame from a list of dicts with keys open/high/low/close[/volume]."""
    idx = pd.date_range(start=start, periods=len(rows), freq=freq)
    df = pd.DataFrame(rows, index=idx)
    if "volume" not in df.columns:
        df["volume"] = 1_000_000
    return df[["open", "high", "low", "close", "volume"]].astype(float)


@pytest.fixture
def bars():
    return make_bars

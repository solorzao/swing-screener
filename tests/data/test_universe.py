from pathlib import Path

from swing_screener.data.universe import UniverseEntry, load_universe

FIXTURE = Path(__file__).parent / "fixtures" / "universe_sample.csv"


def test_loads_and_normalizes():
    entries = load_universe(FIXTURE)
    tickers = [e.ticker for e in entries]
    # uppercased, de-duplicated (order preserved), blank line skipped
    assert tickers == ["AAPL", "MSFT", "NVDA"]
    assert all(isinstance(e, UniverseEntry) for e in entries)
    aapl = entries[0]
    assert aapl.name == "Apple Inc." and aapl.exchange == "NASDAQ"

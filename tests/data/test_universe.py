from pathlib import Path

from swing_screener.data.universe import UniverseEntry, load_universe, names_by_ticker

FIXTURE = Path(__file__).parent / "fixtures" / "universe_sample.csv"


def test_loads_and_normalizes():
    entries = load_universe(FIXTURE)
    tickers = [e.ticker for e in entries]
    # uppercased, de-duplicated (order preserved), blank line skipped
    assert tickers == ["AAPL", "MSFT", "NVDA"]
    assert all(isinstance(e, UniverseEntry) for e in entries)
    aapl = entries[0]
    assert aapl.name == "Apple Inc." and aapl.exchange == "NASDAQ"


def test_names_by_ticker_fixture():
    names = names_by_ticker(FIXTURE)
    # keyed by upper-cased ticker (msft in the CSV becomes MSFT), value is the name
    assert names["AAPL"] == "Apple Inc."
    assert names["MSFT"] == "Microsoft Corp."
    assert names["NVDA"] == "NVIDIA Corp."
    assert set(names) == {"AAPL", "MSFT", "NVDA"}


def test_names_by_ticker_default_seed():
    # exact strings from the shipped universe_seed.csv
    names = names_by_ticker()
    assert names["AAPL"] == "Apple Inc."
    assert names["MSFT"] == "Microsoft"
    assert names["AMD"] == "Advanced Micro Devices"

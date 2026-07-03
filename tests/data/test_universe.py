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


def test_cli_prints_first_n_tickers_comma_joined(monkeypatch, capsys):
    """The workflow seam: `python -m swing_screener.data.universe --first N` prints the
    first N seed tickers as a comma-joined basket for the scheduled optimizer/reflection
    sweeps (widened from the fixed 12-mega-cap list, 2026-07-01 audit)."""
    from swing_screener.data.universe import main

    monkeypatch.setattr("sys.argv", ["universe", "--first", "2", "--path", str(FIXTURE)])
    main()
    assert capsys.readouterr().out.strip() == "AAPL,MSFT"


def test_cli_defaults_to_first_100_of_the_shipped_seed(monkeypatch, capsys):
    from swing_screener.data.universe import main

    monkeypatch.setattr("sys.argv", ["universe"])
    main()
    tickers = capsys.readouterr().out.strip().split(",")
    assert len(tickers) == 100
    assert len(set(tickers)) == 100          # load_universe de-duplicates
    assert all(t == t.upper() and t for t in tickers)

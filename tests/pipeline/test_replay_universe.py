import pandas as pd
import pytest

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import replay_ticker, replay_universe
from tests.pipeline._replay_fixtures import synthetic_daily


def test_two_tickers_concatenate_with_both_tickers_present():
    """Books don't interact: replaying two good tickers yields a single
    concatenated book whose rows carry both ticker labels."""
    bars = {"AAA": synthetic_daily(400), "BBB": synthetic_daily(400)}
    book = replay_universe(bars, StrategyConfig(), warmup_bars=250)

    assert book, "expected some paper trades"
    tickers = {t.ticker for t in book}
    assert tickers == {"AAA", "BBB"}, tickers
    # Each ticker contributed rows (the fixture fires continuation fills).
    assert sum(1 for t in book if t.ticker == "AAA") > 0
    assert sum(1 for t in book if t.ticker == "BBB") > 0


def _broken_frame() -> pd.DataFrame:
    """A frame long enough to clear replay_ticker's warmup early-return guard but
    missing the OHLC columns, so build_frame raises a KeyError once it runs."""
    return pd.DataFrame({"close": [float(i) for i in range(300)]})


def test_broken_frame_actually_raises_inside_replay_ticker():
    """Guard the isolation test below: confirm the deliberately-broken frame
    raises INSIDE replay_ticker (so the except branch is what skips it), rather
    than being silently filtered out before any work happens. It must be long
    enough to pass the warmup early-return, otherwise replay_ticker returns []
    cleanly and never exercises the except path."""
    with pytest.raises(Exception):
        replay_ticker("BAD", _broken_frame(), StrategyConfig(), warmup_bars=250)


def test_per_ticker_isolation_skips_bad_and_keeps_good():
    """One malformed ticker is logged-and-skipped; the good ticker still
    produces rows and the bad one contributes none (exercises the except branch)."""
    bars = {"GOOD": synthetic_daily(400), "BAD": _broken_frame()}

    book = replay_universe(bars, StrategyConfig(), warmup_bars=250)

    assert book, "the good ticker should still produce rows despite the bad one"
    assert {t.ticker for t in book} == {"GOOD"}
    assert all(t.ticker != "BAD" for t in book)


def test_empty_mapping_yields_empty_book():
    assert replay_universe({}, StrategyConfig(), warmup_bars=250) == []

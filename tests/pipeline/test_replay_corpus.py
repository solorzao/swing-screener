"""Corpus pinning: replay evidence must name a reproducible corpus.

``_load_cached_daily`` defaulted to "newest parquet per ticker", which silently mixes
cache vintages as the cache refreshes -- the 2026-07-03 refresh moved prior results and
no edge-file claim could name its corpus. ``as_of`` pins by fetch date; ``corpus_stamp``
reports the vintage histogram and warns on a mixed corpus.
"""

import logging

import pandas as pd

from swing_screener.pipeline.replay import (
    _cached_daily_file,
    _load_cached_daily,
    corpus_stamp,
)


def _write(cache_dir, name, rows=3):
    idx = pd.date_range("2024-01-01", periods=rows, freq="1D")
    df = pd.DataFrame({"open": [1.0] * rows, "high": [2.0] * rows, "low": [0.5] * rows,
                       "close": [float(rows)] * rows, "volume": [100.0] * rows}, index=idx)
    (cache_dir / "1d").mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_dir / "1d" / f"{name}.parquet")


def test_as_of_pins_the_vintage(tmp_path):
    _write(tmp_path, "AMD_20260601", rows=3)
    _write(tmp_path, "AMD_20260703", rows=5)

    assert _cached_daily_file("AMD", tmp_path).name == "AMD_20260703.parquet"  # newest
    assert _cached_daily_file("AMD", tmp_path, as_of="20260615").name == "AMD_20260601.parquet"
    assert _cached_daily_file("AMD", tmp_path, as_of="20260531") is None  # nothing that old

    assert len(_load_cached_daily("AMD", tmp_path)) == 5
    assert len(_load_cached_daily("AMD", tmp_path, as_of="20260615")) == 3
    assert _load_cached_daily("AMD", tmp_path, as_of="20260531") is None


def test_corpus_stamp_reports_vintages_and_warns_on_a_mixed_corpus(tmp_path, caplog):
    _write(tmp_path, "AMD_20260601")
    _write(tmp_path, "NVDA_20260703")
    _write(tmp_path, "AAPL_20260703")

    with caplog.at_level(logging.WARNING, logger="swing_screener.pipeline.replay"):
        stamp = corpus_stamp(tmp_path, ["AMD", "NVDA", "AAPL", "MISSING"])
    assert "3/4 tickers" in stamp
    assert "20260601:1" in stamp and "20260703:2" in stamp
    assert any("MIXED-VINTAGE" in r.message for r in caplog.records)

    # pinned to one vintage: no warning
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="swing_screener.pipeline.replay"):
        stamp = corpus_stamp(tmp_path, ["NVDA", "AAPL"], as_of="20260703")
    assert "2/2 tickers" in stamp and "pinned as-of 20260703" in stamp
    assert not caplog.records

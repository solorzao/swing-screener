import pandas as pd

from swing_screener.indicators.heiken_ashi import heiken_ashi


def test_first_bar_seeds_ha_open_as_avg_of_open_close(bars):
    df = bars([{"open": 10, "high": 12, "low": 9, "close": 11}])
    ha = heiken_ashi(df)
    assert ha["ha_close"].iloc[0] == (10 + 12 + 9 + 11) / 4
    assert ha["ha_open"].iloc[0] == (10 + 11) / 2


def test_subsequent_ha_open_is_avg_of_prev_ha_open_and_close(bars):
    df = bars([
        {"open": 10, "high": 12, "low": 9, "close": 11},
        {"open": 11, "high": 13, "low": 10, "close": 12},
    ])
    ha = heiken_ashi(df)
    expected = (ha["ha_open"].iloc[0] + ha["ha_close"].iloc[0]) / 2
    assert ha["ha_open"].iloc[1] == expected


def test_ha_high_low_envelope(bars):
    df = bars([{"open": 10, "high": 12, "low": 9, "close": 11}])
    ha = heiken_ashi(df)
    assert ha["ha_high"].iloc[0] == max(12, ha["ha_open"].iloc[0], ha["ha_close"].iloc[0])
    assert ha["ha_low"].iloc[0] == min(9, ha["ha_open"].iloc[0], ha["ha_close"].iloc[0])


def test_one_nan_close_poisons_every_subsequent_ha_open(bars):
    """DOCUMENTS the hazard the fetch-seam dropna exists to prevent: the ha_open
    recursion (ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2) never recovers from
    a NaN, so ONE NaN close makes ha_open NaN for every later bar. classify_ha then
    reads bullish=False/bearish=False forever (NaN comparisons are False), so
    detect_last_bar/detect_reversal silently never fire for the ticker -- no error,
    no log, a permanently signal-dead name (the 2026-07-05 NaN-holiday-close class).
    heiken_ashi itself is deliberately left unguarded: _download drops incomplete
    rows so every consumer receives NaN-free OHLC (see tests/data/test_fetch.py)."""
    rows = [{"open": 10.0 + i, "high": 12.0 + i, "low": 9.0 + i, "close": 11.0 + i}
            for i in range(6)]
    rows[2]["close"] = float("nan")
    ha = heiken_ashi(bars(rows))
    assert ha["ha_open"].iloc[:3].notna().all()  # poisoning starts AFTER the NaN bar
    assert ha["ha_open"].iloc[3:].isna().all()   # ...and never, ever recovers


def test_fetch_seam_shields_ha_from_nan_closes(monkeypatch):
    """Seam property (defense in depth for the test above): a download containing a
    NaN close, routed through the shared fetch seam every consumer uses, yields
    NaN-free HA columns because _download drops the incomplete row first."""
    from swing_screener.data import fetch

    idx = pd.date_range("2024-01-01", periods=6, freq="1D")
    raw = pd.DataFrame(
        {"Open": [10.0 + i for i in range(6)], "High": [12.0 + i for i in range(6)],
         "Low": [9.0 + i for i in range(6)], "Close": [11.0 + i for i in range(6)],
         "Volume": [100.0] * 6}, index=idx,
    )
    raw.iloc[2, raw.columns.get_loc("Close")] = float("nan")
    monkeypatch.setattr(fetch.yf, "download", lambda *a, **k: raw)
    ha = heiken_ashi(fetch._download("TEST", "1d", "5y"))
    assert ha.notna().all().all()

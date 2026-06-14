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

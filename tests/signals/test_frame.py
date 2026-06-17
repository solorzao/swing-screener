from swing_screener.config import StrategyConfig
from swing_screener.signals.frame import build_frame


def test_build_frame_has_all_columns(bars):
    df = bars([{"open": 10 + i, "high": 11 + i, "low": 9 + i, "close": 10 + i}
               for i in range(60)])
    f = build_frame(df, StrategyConfig())
    for col in ["ha_open", "ha_close", "ema_fast", "ema_slow", "atr", "rsi",
                "macd_hist", "macd_hist_rising",
                "bullish", "shaved_bottom", "shaved_head", "zone"]:
        assert col in f.columns
    assert len(f) == len(df)

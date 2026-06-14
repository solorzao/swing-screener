import pandas as pd

from swing_screener.config import StrategyConfig


def classify_ha(ha: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    rng = (ha["ha_high"] - ha["ha_low"]).clip(lower=1e-12)
    body = (ha["ha_close"] - ha["ha_open"]).abs()
    top = ha[["ha_open", "ha_close"]].max(axis=1)
    bot = ha[["ha_open", "ha_close"]].min(axis=1)
    upper_wick = ha["ha_high"] - top
    lower_wick = bot - ha["ha_low"]

    bullish = ha["ha_close"] > ha["ha_open"]
    bearish = ha["ha_close"] < ha["ha_open"]
    tol = cfg.wick_frac * rng

    return pd.DataFrame({
        "bullish": bullish,
        "bearish": bearish,
        "shaved_bottom": bullish & (lower_wick <= tol),
        "shaved_head": bearish & (upper_wick <= tol),
        "zone": body <= cfg.zone_body_frac * rng,
        "body_frac": body / rng,
    }, index=ha.index)

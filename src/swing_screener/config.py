from dataclasses import dataclass, field


@dataclass(frozen=True)
class StrategyConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # HA classification
    wick_frac: float = 0.05        # "no wick" tolerance as fraction of range
    zone_body_frac: float = 0.30   # doji/zone: body <= frac * range

    # pullback
    min_pullback_bars: int = 1
    max_pullback_bars: int = 4

    # entry zone / risk
    ceiling_atr_mult: float = 0.35   # ceiling = trigger_close + mult * ATR
    floor_buffer_atr: float = 0.10   # floor = swing_low + buffer * ATR
    stop_buffer_atr: float = 0.25    # stop  = swing_low - buffer * ATR

    # structure-aware continuation target (Step A)
    target_lookback: int = 30        # bars to search for overhead resistance
    target_pivot_width: int = 2      # a swing high tops this many bars on each side
    target_atr_mult: float = 2.0     # measured-move fallback when no resistance overhead
    min_target_r: float = 1.5        # floor: target is at least this many R above the reference

    # categorization tag thresholds (tunable for the dry-run)
    penny_price_max: float = 5.0            # price < this -> "penny"
    speculative_dollar_vol_max: float = 5e6   # avg $vol < this -> "speculative"
    mid_dollar_vol_max: float = 50e6        # avg $vol < this -> "mid", else "reputable"
    low_vol_atr_pct_max: float = 0.02       # atr% < this -> "low"
    med_vol_atr_pct_max: float = 0.05       # atr% < this -> "med", else "high"
    oversold_rsi_max: float = 35.0          # rsi < this -> oversold
    avg_dollar_vol_window: int = 20         # bars used for avg dollar volume

    # reversal screener (Heiken-Ashi reversal off a downtrend -> the "Reversal Plays" list).
    # HA structure is the gate; RSI is only a scoring confirmation (NOT a hard filter).
    reversal_min_bearish_bars: int = 3        # HA red bars required in the decline (downtrend gate)
    reversal_oversold_rsi_max: float = 25.0   # RSI scoring floor: how deep is "deeply oversold"
    reversal_oversold_lookback: int = 5       # bars to look back when measuring oversold depth
    reversal_decline_bars: int = 6            # window for the decline low (the bounce origin)
    reversal_target_lookback: int = 30        # bars for the prior decline high (the target/breakdown)
    # Reversal entry is a PULLBACK into the bounce (a limit below the chase price), so
    # there's room to both stop and target. The zone is this retracement band of the
    # bounce (reversal_low -> bounce_high):
    reversal_pullback_shallow: float = 0.382  # shallow end of the pullback entry zone
    reversal_pullback_deep: float = 0.618     # deep end of the pullback entry zone
    reversal_retrace_frac: float = 0.786      # target = this retracement of the decline (toward breakdown)
    reversal_target_r_multiple: float = 1.0   # measured-move fallback if the target sits below entry

    # exits
    time_stop_factor: float = 1.0    # time stop = factor * max_hold_bars[tf]
    max_hold_bars: dict[str, int] = field(default_factory=lambda: {
        "4h": 18,   # ~3 trading days of 4h bars
        "1d": 10,
        "1wk": 8,
        "1mo": 6,
    })

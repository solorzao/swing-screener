import numpy as np
import pandas as pd


def synthetic_daily(n: int = 400) -> pd.DataFrame:
    """Deterministic daily OHLCV: a long uptrend with periodic shallow pullbacks
    (continuation triggers) plus one deep decline+bounce section.

    The constants are tuned so the HA pullback engine actually fires on this
    synthetic series (see _replay_fixtures tuning notes in the replay PR):
      * slope 0.08/bar keeps EMA50 below each pullback trough, so the swing low
        stays above ema_slow -- the shallow-pullback gate detect_last_bar requires.
      * a 1.5-amplitude sine with period 8 makes the rhythmic dips steep enough to
        print contiguous bearish HA bars (with a shaved head) before snapping back
        to a bullish trigger bar.
      * a thin 0.05 wick means the down bars have ~no upper wick, so they classify
        as shaved_head (bearish & upper_wick <= wick_frac*range) -- the trigger
        quality the detector looks for inside the pullback.
    Replayed with warmup_bars=250, this fires 7 CONTINUATION fills across 400 bars
    -- enough overlapping fills for the no-lookahead test. The deep dip is too
    shallow/brief to satisfy the reversal gate, so it produces NO reversal triggers;
    the no-lookahead invariant holds every step regardless of which plays fire.
    """
    idx = pd.bdate_range("2024-01-01", periods=n)
    t = np.arange(n)
    base = 50 + 0.08 * t                                  # gentle uptrend
    pullback = 1.5 * np.sin(t / 8.0)                       # rhythmic pullbacks
    dip = np.where((t > n // 2) & (t < n // 2 + 12), -6.0, 0.0)  # one sharp decline
    close = base + pullback + dip
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.05
    low = np.minimum(open_, close) - 0.05
    vol = np.full(n, 1_000_000.0)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": vol}, index=idx)

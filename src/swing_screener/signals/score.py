from dataclasses import dataclass

_RSI_BULL = 50.0
_RSI_FLOOR = 40.0


@dataclass(frozen=True)
class ScoreInputs:
    shaved_bottom: bool
    body_frac: float       # trigger body / range, 0..1
    trend_slope: float     # normalized (ema_fast - ema_slow) / price, clipped
    atr_pct: float         # ATR / price
    mtf_aligned: bool
    # Defaults are conservative/neutral so a caller that omits them never FLATTERS a
    # signal (production always sets all three from the trigger bar via build_score).
    # rsi 45.0 -> _rsi_quality 0.5 (no opinion), not 50.0 which would grant full credit.
    rsi: float = 45.0              # trigger-bar RSI (bull-range pullback quality)
    macd_hist: float = 0.0         # trigger-bar MACD histogram (0 -> _hist_accel 0)
    macd_hist_rising: bool = False  # histogram > prior bar's histogram


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _rsi_quality(rsi: float) -> float:
    """Bull-range pullback quality: 1.0 at/above 50, linear 40->50, 0 below 40."""
    if rsi >= _RSI_BULL:
        return 1.0
    if rsi <= _RSI_FLOOR:
        return 0.0
    return (rsi - _RSI_FLOOR) / (_RSI_BULL - _RSI_FLOOR)


def _hist_accel(macd_hist: float, rising: bool) -> float:
    """MACD histogram momentum: 1.0 positive+rising, 0.5 positive+not-rising, 0 non-positive."""
    if macd_hist <= 0:
        return 0.0
    return 1.0 if rising else 0.5


def score_signal(s: ScoreInputs) -> float:
    strength = 0.6 * _clip01(s.body_frac) + 0.4 * (1.0 if s.shaved_bottom else 0.0)
    slope = _clip01(s.trend_slope * 20.0)           # ~0.05 slope -> 1.0
    vol_fit = _clip01(s.atr_pct / 0.04)             # reward some volatility, saturate at 4%
    mtf = 1.0 if s.mtf_aligned else 0.0
    rsi_q = _rsi_quality(s.rsi)
    hist = _hist_accel(s.macd_hist, s.macd_hist_rising)
    score = (0.35 * strength + 0.20 * mtf + 0.15 * slope
             + 0.10 * vol_fit + 0.15 * rsi_q + 0.05 * hist)
    return _clip01(score)

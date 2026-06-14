from dataclasses import dataclass


@dataclass(frozen=True)
class ScoreInputs:
    shaved_bottom: bool
    body_frac: float       # trigger body / range, 0..1
    trend_slope: float     # normalized (ema_fast - ema_slow) / price, clipped
    atr_pct: float         # ATR / price
    mtf_aligned: bool


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_signal(s: ScoreInputs) -> float:
    strength = 0.6 * _clip01(s.body_frac) + 0.4 * (1.0 if s.shaved_bottom else 0.0)
    slope = _clip01(s.trend_slope * 20.0)           # ~0.05 slope -> 1.0
    vol_fit = _clip01(s.atr_pct / 0.04)             # reward some volatility, saturate at 4%
    mtf = 1.0 if s.mtf_aligned else 0.0
    score = 0.40 * strength + 0.25 * mtf + 0.20 * slope + 0.15 * vol_fit
    return _clip01(score)

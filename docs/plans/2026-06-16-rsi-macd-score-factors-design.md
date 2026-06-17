# Continuation score: RSI + MACD-histogram momentum factors — design

**Date:** 2026-06-16
**Status:** Approved
**Scope:** Phase 1 of the target/indicator overhaul (the two "cheap scorers"). Pure
score-factor additions to the CONTINUATION engine only — NO target/exit/shadow-book
refactor. See [[project-target-indicator-overhaul]] for the full roadmap.

## Goal

Fold two momentum confirmations into the continuation score: an RSI **bull-range
pullback-quality** factor (the already-computed-but-idle RSI) and a MACD **histogram
acceleration** factor. Both are pure functions over the enriched frame, feed the shadow
book, and are judged by `expectancy_r`.

## Changes

### 1. RSI pullback-quality factor (`rsi_quality`)
Source: `ctx.rsi` (the trigger-bar RSI, already on `PullbackContext`). Bull-range read
(Cardwell): on the resumption bar a shallow pullback should hold the bull range.
```
rsi >= 50            -> 1.0
40 <= rsi < 50       -> (rsi - 40) / 10        # 45 -> 0.5
rsi < 40             -> 0.0                     # bull range broken: soft de-prioritization
```
NOT a hard gate (that would double-count HA + EMA20>EMA50). A 0-contribution at <40 is a
soft veto within the additive score; a stronger negative penalty is a later tunable.

### 2. MACD histogram-acceleration factor (`hist_accel`)
- `indicators/trend.py`: add `macd_histogram(close, fast=12, slow=26, signal=9) -> Series`
  = `ema(close,12) - ema(close,26)` minus its 9-EMA signal line (reuse the existing `ema`).
- `frame.py`: add `macd_hist` and `macd_hist_rising` (= `macd_hist > macd_hist.shift(1)`).
- Factor (read off the trigger row):
```
hist > 0 and rising        -> 1.0
hist > 0 and not rising     -> 0.5
hist <= 0                   -> 0.0
```
The histogram is the 2nd derivative of the EMA spread — orthogonal to the existing static
`trend_slope = (EMA20-EMA50)/price` level. Small weight; let the shadow book earn it.

### 3. Rebalanced continuation weights (`score.py`, sum = 1.0)
| factor | old | new |
|---|---|---|
| strength (HA body+shaved) | 0.40 | 0.35 |
| mtf_aligned | 0.25 | 0.20 |
| trend_slope | 0.20 | 0.15 |
| vol_fit (atr%) | 0.15 | 0.10 |
| **rsi_quality** | — | **0.15** |
| **hist_accel** | — | **0.05** |

`ScoreInputs` gains `rsi: float` and `hist_accel: float`; `build_score_inputs` populates
`rsi` from `ctx.rsi` and `hist_accel` from `last_row["macd_hist"]`/`["macd_hist_rising"]`.

## Out of scope
Reversal engine (RSI already used there; unchanged this step), targets/partials/trails (the
blocked Phase-2/3 work), MACD line/cross (rejected as redundant), divergence, Alligator, VWAP.

## Test ripple (must stay green)
Changing the weights changes scores → ranks. Update expected values in `tests/signals/test_score.py`
and `tests/test_golden_amd.py` (and any other score-asserting test), and add tests for
`macd_histogram`, the two new score factors, the rebalanced `score_signal`, and the new frame
columns. The reversal score is untouched.

## QC
This ships the new scoring as the default. A clean A/B of new-vs-old weights against shadow-book
`expectancy_r` is a separate measurement the user can run on the same universe/date range.

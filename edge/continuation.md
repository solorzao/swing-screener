---
forward_closed_at_last_reflection: 472
last_reflected: 2026-07-02
---

> This playbook is maintained by the **reflection** pass (`pipeline/reflect.py`): code deterministically grades each pre-registered condition into a tiered verdict and an Opus seam authors the prose. Every change lands as a **human-gated PR** -- nothing here is auto-merged. The file is also **hand-editable**.

## Thesis

Heiken-Ashi pullback-continuation: an established uptrend, a shallow HA pullback, enter
long on the bullish HA flip out of the pullback zone.

The intuition is that a smoothed HA candle series filters intrabar noise, so a run of
green HA candles followed by a shallow red pause and a fresh green flip should mark a
low-risk re-entry into a trend already in motion. That's the hypothesis. So far the
evidence does **not** support it: every slice we've measured sits at or below breakeven
net of cost, and nothing has cleared its corrected lower bound on either book.

## Confirmed edges

_Forward-confirmed (gold): cleared the multiple-comparisons-corrected lower bound on the live forward shadow book._

_none yet_

## Screened candidates

_Replay-screened (candidate): cleared the bound on the haircut replay corpus only -- a backtest screen, NOT live-confirmed._

_none yet_

## Hunches / needs a test

_Watched conditions that have not cleared the bound on either book -- ideas, not edges._

Everything below is a hunch, not an edge. None of these slices clears its clustered
95% CI lower bound; all are quoted net of cost with an optimistic-fill haircut applied.
Read the expectancies as "where the mass currently sits," not as a signal to trade.

- **Bull-market entries** are the whole sample right now, and they're negative:
  expectancy -0.17R, n=231 (138 tickers), clustered 95% CI lower bound -0.36R. The
  core continuation thesis is meant to live in exactly this regime, so a negative
  central estimate on the largest slice is the most important warning sign in the file.
- **Bear-market entries**: expectancy +0.00R, n=0 (0 tickers), clustered 95% CI lower
  bound +0.00R. No observations -- nothing to say yet. A pullback-continuation long has
  no natural home in a downtrend, so this may simply stay empty.

Score buckets (the model's own conviction) do not sort outcomes in the direction we'd
hope -- higher scores are, if anything, worse:

- **score=0.50-0.60**: expectancy +0.13R, n=98 (71 tickers), clustered 95% CI lower
  bound -0.18R. The only positive central estimate in the file, but the lower bound is
  still under water -- a coin-flip we can't yet distinguish from noise.
- **score=0.00-0.50**: expectancy -0.30R, n=87 (52 tickers), clustered 95% CI lower
  bound -0.60R.
- **score=0.60-0.70**: expectancy -0.15R, n=148 (72 tickers), clustered 95% CI lower
  bound -0.43R.
- **score=0.70-0.80**: expectancy -0.44R, n=65 (46 tickers), clustered 95% CI lower
  bound -0.66R. The worst score bucket -- high conviction, worst outcome.
- **score=0.80-1.00**: expectancy -0.26R, n=74 (34 tickers), clustered 95% CI lower
  bound -0.55R.

The inverted score gradient (0.70+ underperforming 0.50-0.60) is a red flag that the
scoring feature is picking up something that predicts the *wrong* direction. Worth a
dedicated test (see Open questions).

Volatility tiers all sit negative, with the damage concentrated in high-vol names:

- **volatility_tier=high**: expectancy -0.42R, n=128 (37 tickers), clustered 95% CI
  lower bound -0.68R. Wide stops and gappy fills likely eat the edge here.
- **volatility_tier=med**: expectancy -0.06R, n=204 (95 tickers), clustered 95% CI
  lower bound -0.26R. The least-bad tier and the deepest sample -- the closest thing to
  a survivable slice, but still not clearing the bound.
- **volatility_tier=low**: expectancy -0.12R, n=140 (91 tickers), clustered 95% CI
  lower bound -0.33R.

## Falsified / retired

_Prior claims now contradicted by the evidence, kept for the record._

_none yet_

## Analyst calibration

_Code-owned report card on the analyst's conviction calls (scored shadow-book outcomes): does its judgment prove out?_

_No scored analyst calls yet -- calibration pending._
## Open questions

_Things to investigate next._

- Why is the score gradient inverted? The 0.70-0.80 and 0.80-1.00 buckets are the worst
  performers while 0.50-0.60 is the only positive central estimate. If high conviction
  reliably predicts worse outcomes, the scoring feature may be anti-correlated with the
  real edge -- test whether inverting or ablating the score improves selection.
- Does excluding high-volatility names (n=128, -0.42R central) lift the rest of the book
  toward breakeven, or is the weakness spread evenly? Test a med-only cut.
- Is "shallow pullback" being measured tightly enough? Consider testing pullback-depth
  and trend-strength (e.g. distance above a rising MA) as pre-registered gates before the
  HA flip, to see whether a stricter continuation filter separates winners from losers.
- The bear-market slice is empty (n=0). Is that by construction (no signals fire) or a
  data gap? Confirm the regime filter is actually admitting bear samples before drawing
  any conclusion.

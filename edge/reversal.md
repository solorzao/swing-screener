---
forward_closed_at_last_reflection: 506
last_reflected: 2026-07-02
---

> This playbook is maintained by the **reflection** pass (`pipeline/reflect.py`): code deterministically grades each pre-registered condition into a tiered verdict and an Opus seam authors the prose. Every change lands as a **human-gated PR** -- nothing here is auto-merged. The file is also **hand-editable**.

## Thesis

Oversold Heiken-Ashi reversal: a downtrend washout showing a green-out-of-red HA flip; enter on a pullback into the bounce, not a chase.

The core intuition is that a Heiken-Ashi color flip after a sustained downleg marks the moment sellers exhaust and the tape smooths into a bounce. The discipline is in the entry: we wait for a pullback *into* the nascent bounce rather than chasing the first green candle, so our risk is defined against the washout low. This file tracks which market contexts actually pay for that patience once costs are honest.

## Confirmed edges

_Forward-confirmed (gold): cleared the multiple-comparisons-corrected lower bound on the live forward shadow book._

- **Low-volatility names (`volatility_tier=low`)** — expectancy **+0.35R**, n=113 (85 tickers), clustered 95% CI lower bound **+0.05R**, net of cost (optimistic-fill haircut applied).

  This is the one context that has held up on the live forward book. The read: in calm, low-volatility names the HA flip is a genuine tell rather than noise, because there isn't enough churn to manufacture false flips. The pullback entry gets filled at a sensible level, and the bounce carries far enough to clear the cost haircut with room to spare. The CI lower bound sits just above zero, so the edge is real but thin — size it as a steady contributor, not a hero trade, and respect that the margin leaves little slack for slippage beyond the modeled haircut.

## Screened candidates

_Replay-screened (candidate): cleared the bound on the haircut replay corpus only -- a backtest screen, NOT live-confirmed._

- **Windowed confirmation (`reversal_confirm_window=3`)** — confirmed-cohort expectancy **+0.039R**, n=9,706 closed (511 clusters), clustered 95% CI lower bound **+0.012R**, net of 0.05 ATR slippage, full 511-name / 5y corpus (through 2026-07-02). The legacy next-bar-only rule scored +0.004R (lower bound -0.028R) on the same corpus — it no longer clears the bar there.

  The isolated **late-confirm increment** (flips that confirmed 2-3 bars after the flip — permanently invisible to the legacy rule) is the strongest cohort measured on this book: **+0.110R**, n=7,833 (3,191 closed, 507 clusters), clustered lower bound **+0.065R** net of cost. Mechanism: a pause bar before confirmation leaves the pullback limit near the market (41% fill vs the vertical confirms it replaces), and a digested confirmation is higher-quality than a one-bar rip. Found while diagnosing the missed 2026-07-01/02 software rotation (INTU missed legacy confirmation by $0.80 on Jul 2 and the setup died forever). Robustness at 0.10 ATR slippage: see the run log in the PR; forward confirmation accrues via the default book vs the `rev_confirm1` legacy screen variant.

## Hunches / needs a test

_Watched conditions that have not cleared the bound on either book -- ideas, not edges._

- **Bear-market regime (`market_trend=bear`)** — expectancy +0.55R, n=17 (8 tickers), clustered 95% CI lower bound **-0.42R**, net of cost. The headline expectancy is the highest on the board and it fits the thesis nicely — oversold reversals should pay best when the broad tape is scared. But n=17 across only 8 tickers is far too thin to trust; the lower bound is deeply negative. Tantalizing, not tradeable. Keep collecting bear-regime samples.

- **Bull-market regime (`market_trend=bull`)** — expectancy +0.14R, n=288 (182 tickers), clustered 95% CI lower bound **-0.12R**, net of cost. This is where most of the sample lives. Modestly positive expectancy but the lower bound is still under water. Reversal setups in a bull tape may just be shallow dip-buys that don't develop into much. Watch whether the low-volatility edge is actually driving this bucket.

- **Score 0.00–0.50 (`score=0.00-0.50`)** — expectancy +0.02R, n=493 (234 tickers), clustered 95% CI lower bound **-0.15R**, net of cost. The bulk of scored setups cluster here and grade out to roughly breakeven before the CI is accounted for. The scoring model is not yet separating winners from losers in its low band.

- **Score 0.50–0.60 (`score=0.50-0.60`)** — expectancy -0.21R, n=12 (12 tickers), clustered 95% CI lower bound **-0.94R**, net of cost. Tiny, negative. No signal.

- **Score 0.60–0.70 (`score=0.60-0.70`)** — expectancy -1.00R, n=1 (1 tickers), clustered 95% CI lower bound **-1.00R**, net of cost. A single losing trade. Ignore until there's a sample.

- **Score 0.70–0.80 (`score=0.70-0.80`)** — expectancy +0.00R, n=0, clustered 95% CI lower bound +0.00R, net of cost. No observations yet.

- **Score 0.80–1.00 (`score=0.80-1.00`)** — expectancy +0.00R, n=0, clustered 95% CI lower bound +0.00R, net of cost. No observations yet. The high-conviction band is empty — either the scorer rarely fires this high or those setups haven't materialized in the window.

- **High-volatility names (`volatility_tier=high`)** — expectancy -0.22R, n=124 (48 tickers), clustered 95% CI lower bound **-0.49R**, net of cost. The direct counterpoint to the confirmed low-vol edge: in high-volatility names the HA flip is unreliable, the pullback entry gets whipped, and expectancy is negative. Current evidence argues against taking this setup in fast, noisy names.

- **Medium-volatility names (`volatility_tier=med`)** — expectancy -0.02R, n=269 (164 tickers), clustered 95% CI lower bound **-0.24R**, net of cost. Essentially breakeven and below the bound. The edge fades as volatility rises off the low tier — med sits in no-man's-land.

## Falsified / retired

_Prior claims now contradicted by the evidence, kept for the record._

- **Chase entries (`reversal_ceiling_at_close`, ceiling at the trigger close)** — REFUTED 2026-07-03: fill rate jumps 44% -> 92% but confirmed-cohort expectancy collapses to **-0.062R** (clustered lower bound -0.075R, n=13,657 closed), and the rotation cohort is no better. The pullback limit's "adverse selection" is actually favorable selection — buying lower IS the edge; paying up forfeits it. The V-rotation fix is the confirmation WINDOW (catch the pause-then-confirm shape), not a chase.
- **Confirmation-bar band anchor (`reversal_anchor_confirmation`)** — REFUTED 2026-07-03: raising the band onto the bounce top lifts fills (44% -> 61%) but drops confirmed expectancy to -0.022R (lower bound -0.043R). Same lesson as the chase, in miniature.
- **Corpus note**: the 2026-07-03 refresh (511 names, 5y through 2026-07-02, current yfinance adjustments) moved several old numbers — legacy CONFIRMED no longer clears the bound standalone (+0.004R vs the +0.125R measured 2026-06-25), and the old "confirmed does best at VIX>70" read INVERTED (vix_bucket=high -0.068R, vix_bucket=low +0.115R lower bound +0.063R on this corpus). Prior-corpus claims should be re-validated before promotion decisions lean on them.

## Analyst calibration

_Code-owned report card on the analyst's conviction calls (scored shadow-book outcomes): does its judgment prove out?_

_No scored analyst calls yet -- calibration pending._
## Open questions

_Things to investigate next._

- EARLY signals graded **+0.020R overall (clustered lower bound +0.005R)** on the refreshed corpus — thin but positive, unlike the 2026-06-25 read. The strong sub-cohorts (EARLY × vol_tier=high +0.171R lb +0.108; EARLY × market_vol=high +0.209R lb +0.172) come from a wide multiple-comparison sweep and contradict the forward-confirmed low-vol edge's direction — treat as hunches for the reflection loop, not surfacing changes.
- Same-day sector clustering (>=4 reversal signals in one GICS sector) tags ~85% of the book — reversal signals inherently cluster on broad red days, so the tag as defined barely discriminates. A sharper rotation definition (sector-relative return of the CLUSTER vs SPY on the flip day, or sector-ETF thrust conditioning) is the next cut worth building; the surfacing sector cap already handles the crowding symptom.

- Is the confirmed low-volatility edge doing all the work inside the bull-regime bucket? Cross-tabulate `volatility_tier=low` against `market_trend=bull` to see whether the two are the same trades wearing different labels.
- The volatility gradient is monotonic and clean (low +0.35R → med -0.02R → high -0.22R). Worth testing whether a hard volatility filter — trade only the low tier — is the single most important gate for this play.
- The scoring model shows no separation in its populated bands and the high-conviction bands (0.70+) are empty. Does the scorer ever fire high on reversal setups, and if so does it predict anything?
- Bear-regime expectancy is the highest observed but the sample is tiny. Prioritize accumulating bear-tape samples, since the thesis predicts this is where oversold reversals should shine.

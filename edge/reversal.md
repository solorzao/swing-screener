---
forward_closed_at_last_reflection: 523
last_reflected: 2026-07-05
---

> This playbook is maintained by the **reflection** pass (`pipeline/reflect.py`): code deterministically grades each pre-registered condition into a tiered verdict and an Opus seam authors the prose. Every change lands as a **human-gated PR** -- nothing here is auto-merged. The file is also **hand-editable**.

## Thesis

Oversold Heiken-Ashi reversal: a downtrend washout showing a green-out-of-red HA flip; enter on a pullback into the bounce, not a chase.

The core intuition is that a Heiken-Ashi color flip after a sustained downleg marks the moment sellers exhaust and the tape smooths into a bounce. The discipline is in the entry: we wait for a pullback *into* the nascent bounce rather than chasing the first green candle, so our risk is defined against the washout low. This file tracks which market contexts actually pay for that patience once costs are honest.

## Confirmed edges

_Forward-confirmed (gold): cleared the multiple-comparisons-corrected lower bound on the live forward shadow book._

_none yet_

Nothing currently clears the corrected lower bound on the live forward book. Note that the prior file carried a forward-confirmed **low-volatility** edge (+0.35R, n=113); on this refreshed corpus the low-vol bucket has fallen back to a hunch (see below). That is a demotion driven by the data, not a retirement of the idea — the low-vol thesis is still the most promising thread, it simply no longer clears the gold bar on the current book. Treat the Confirmed shelf as genuinely empty until a bucket re-earns it live.

## Screened candidates

_Replay-screened (candidate): cleared the bound on the haircut replay corpus only -- a backtest screen, NOT live-confirmed._

- **Bear-market regime (`market_trend=bear`)** — expectancy **+0.20R**, n=2387 (100 tickers), clustered 95% CI lower bound **+0.10R**, net of cost (optimistic-fill haircut applied).

  This is the cleanest screen on the board and it lands exactly where the thesis predicts: oversold HA reversals pay best when the broad tape is scared. The bear bucket that was a tantalizing-but-untradeable n=17 hunch in the prior file has now filled out to n=2387 across 100 tickers, and the expectancy held up with a lower bound comfortably above zero. Mechanism: in a bear tape genuine washouts are frequent, sellers overshoot, and the pullback entry gets filled into real capitulation rather than shallow noise. Still backtest-only — needs to prove out on the live forward book before it earns gold — but it is the leading promotion candidate.

- **Low score band (`score=0.00-0.50`)** — expectancy **+0.06R**, n=8447 (100 tickers), clustered 95% CI lower bound **+0.01R**, net of cost (optimistic-fill haircut applied).

  The bulk of scored setups live in this band, and after the v2 score re-weighting it now grades marginally positive with a lower bound just above zero on the replay corpus. The margin is thin — this is a "the low band is not a filter to avoid" result more than a genuine edge. Do not read it as the scorer separating winners; read it as the base population clearing the bar by a hair. Watch whether it survives forward.

- **High-volatility names (`volatility_tier=high`)** — expectancy **+0.24R**, n=531 (42 tickers), clustered 95% CI lower bound **+0.09R**, net of cost (optimistic-fill haircut applied).

  This is a genuine surprise and a direct reversal of the prior file's read, where high-vol was a hunch with negative expectancy (-0.22R, lower bound -0.49R). On the refreshed corpus and with the confirmation-window default in place, high-vol names now screen positively — the washout-into-bounce shape appears to carry further in fast names once the pause-then-confirm entry keeps us out of the worst whipsaws. Note the tension: this cuts *against* the old low-vol thesis. Both cannot be the whole story; see Open questions. Backtest screen only — treat with extra caution given how recently the sign flipped.

## Hunches / needs a test

_Watched conditions that have not cleared the bound on either book -- ideas, not edges._

- **Bull-market regime (`market_trend=bull`)** — expectancy +0.02R, n=5498 (100 tickers), clustered 95% CI lower bound **-0.04R**, net of cost. Where most of the sample lives, and it grades to roughly breakeven with the lower bound still under water. Reversal setups in a bull tape look like shallow dip-buys that don't develop. The contrast with the bear screen is the story: this play wants a scared tape.

- **Low-volatility names (`volatility_tier=low`)** — expectancy +0.05R, n=1950 (72 tickers), clustered 95% CI lower bound **-0.06R**, net of cost. The former forward-confirmed edge, now demoted on the refreshed/expanded corpus: expectancy collapsed from +0.35R (n=113) to +0.05R (n=1950) and the lower bound slipped just under zero. The larger sample is the honest read — the small-n confirmed number was likely flattered. Still directionally positive and worth watching, but no longer an edge and now in direct tension with the high-vol screen.

- **Medium-volatility names (`volatility_tier=med`)** — expectancy +0.05R, n=6458 (100 tickers), clustered 95% CI lower bound **-0.01R**, net of cost. The largest volatility bucket, sitting a whisker below the bar. Breakeven-plus with a lower bound that just misses. No-man's-land, as before.

- **Score 0.50–0.60 (`score=0.50-0.60`)** — expectancy +0.19R, n=291 (97 tickers), clustered 95% CI lower bound **-0.01R**, net of cost. Encouraging headline expectancy and the lower bound is now only just under zero — a big improvement over the prior file's tiny negative sample. Closest of the score bands to graduating. Keep accumulating.

- **Score 0.60–0.70 (`score=0.60-0.70`)** — expectancy +0.02R, n=153 (74 tickers), clustered 95% CI lower bound **-0.27R**, net of cost. Breakeven headline, wide negative bound. No signal yet.

- **Score 0.70–0.80 (`score=0.70-0.80`)** — expectancy +0.06R, n=39 (31 tickers), clustered 95% CI lower bound **-0.47R**, net of cost. Thin and noisy; the bound is deeply negative. Ignore until the sample grows.

- **Score 0.80–1.00 (`score=0.80-1.00`)** — expectancy -0.25R, n=9 (9 tickers), clustered 95% CI lower bound **-1.02R**, net of cost. The high-conviction band finally has observations — nine of them, and they lost. Far too thin to conclude anything, but the scorer firing high is not yet a mark of quality.

## Falsified / retired

_Prior claims now contradicted by the evidence, kept for the record._

- **Chase entries (`reversal_ceiling_at_close`, ceiling at the trigger close)** — REFUTED 2026-07-03: fill rate jumps 44% -> 92% but confirmed-cohort expectancy collapses to **-0.062R** (clustered lower bound -0.075R, n=13,657 closed), and the rotation cohort is no better. The pullback limit's "adverse selection" is actually favorable selection — buying lower IS the edge; paying up forfeits it. The V-rotation fix is the confirmation WINDOW (catch the pause-then-confirm shape), not a chase.
- **Confirmation-bar band anchor (`reversal_anchor_confirmation`)** — REFUTED 2026-07-03: raising the band onto the bounce top lifts fills (44% -> 61%) but drops confirmed expectancy to -0.022R (lower bound -0.043R). Same lesson as the chase, in miniature.
- **Legacy reversal score (0.35 bounce / 0.20 downtrend / 0.15 volume / 0.15 confirmed / 0.15 depth)** — FALSIFIED as an ordering, 2026-07-03 rank sweep over the fixed confirmed replay book (n=9,673 closed): flat quintile ladder (+0.011 → +0.038), top-quintile-vs-rest clustered delta lower bound **-0.059**. Every individual component also failed to separate (bounce -0.060, downtrend -0.063, RSI depth -0.045, spring -0.048, volume alone -0.047); ranking calm names first actually inverted. The only ordering with a positive clustered separation bound at BOTH cost levels is **confirmation lag** (+0.030 at 0.05 slippage / +0.034 at 0.10), with flip volume additive (+0.043 / +0.048). The score was re-weighted accordingly (v2: 0.40 lag / 0.30 volume / 0.10 bounce / 0.10 downtrend / 0.10 confirmed / 0 depth, validated top-quintile bound +0.043 and the best per-day top-5 simulation of all keys tested, +0.073R lower bound +0.019). **Score-band history before 2026-07-03 measures the OLD definition** — treat the score dimension's accumulated verdicts as reset.
- **Corpus note**: the 2026-07-03 refresh (511 names, 5y through 2026-07-02, current yfinance adjustments) moved several old numbers — legacy CONFIRMED no longer clears the bound standalone (+0.004R vs the +0.125R measured 2026-06-25), and the old "confirmed does best at VIX>70" read INVERTED (vix_bucket=high -0.068R, vix_bucket=low +0.115R lower bound +0.063R on this corpus). Prior-corpus claims should be re-validated before promotion decisions lean on them.
- **Note — low-vol de-confirmation (this pass):** the prior file's forward-confirmed low-volatility edge (+0.35R, n=113) did not survive corpus expansion — it now sits at +0.05R, n=1950, lower bound -0.06R and has been moved to Hunches. Recorded here as a caution against small-n confirmations, not as a fully refuted idea; the low-vol thread is still live but unproven.

## Analyst calibration

_Code-owned report card on the analyst's conviction calls (scored shadow-book outcomes): does its judgment prove out?_

_No scored analyst calls yet -- calibration pending._
## Open questions

- **The volatility sign flipped — which read is real?** The prior corpus said low-vol wins and high-vol loses; the refreshed corpus says high-vol screens **+0.24R (lb +0.09, n=531)** while low-vol has faded to a hunch (+0.05R, lb -0.06, n=1950). These directly contradict. Is the high-vol result driven by the confirmation-window default keeping us out of whipsaws, by the corpus refresh, or by the bear-regime overlap (high-vol names cluster on scared days)? Cross-tabulate `volatility_tier=high` against `market_trend=bear` before leaning on either.
- **Is the bear screen and the high-vol screen the same trades?** Both are the standout positive buckets and both fit "this play wants a scared tape." Cross-tab bear × high-vol to see whether one label is doing all the work.
- **Prioritize forward samples for the two live screens (bear, high-vol).** Both cleared the replay bound with healthy n; the next milestone is the live forward book. Do not surface on these until they clear gold.
- The scoring model (v2: 0.40 lag / 0.30 volume) shows its best signal in the **0.50–0.60** band (lb -0.01, nearly clearing) but the top bands (0.70+) are thin and not yet predictive, with the 0.80–1.00 band actually negative on n=9. Does the scorer's high end ever earn its conviction, or is the useful signal concentrated in the mid-band?
- EARLY signals graded **+0.020R overall (clustered lower bound +0.005R)** on the prior refreshed corpus — thin but positive. The strong sub-cohorts (EARLY × vol_tier=high; EARLY × market_vol=high) came from a wide multiple-comparison sweep and align with the *new* high-vol screen's direction while contradicting the *old* low-vol confirmed edge. Now that high-vol screens positive, re-examine whether EARLY × high-vol is a coherent single edge.
- Same-day sector clustering (>=4 reversal signals in one GICS sector) tags ~85% of the book — reversal signals inherently cluster on broad red days, so the tag as defined barely discriminates. A sharper rotation definition (sector-relative return of the CLUSTER vs SPY on the flip day, or sector-ETF thrust conditioning) is the next cut worth building; the surfacing sector cap already handles the crowding symptom.

---
forward_closed_at_last_reflection: 663
last_reflected: 2026-07-12
---

> This playbook is maintained by the **reflection** pass (`pipeline/reflect.py`): code deterministically grades each pre-registered condition into a tiered verdict and an Opus seam authors the prose. Every change lands as a **human-gated PR** -- nothing here is auto-merged. The file is also **hand-editable**.

## Thesis

Oversold Heiken-Ashi reversal: a downtrend washout showing a green-out-of-red HA flip; enter on a pullback into the bounce, not a chase.

The core intuition is that a Heiken-Ashi color flip after a sustained downleg marks the moment sellers exhaust and the tape smooths into a bounce. The discipline is in the entry: we wait for a pullback *into* the nascent bounce rather than chasing the first green candle, so our risk is defined against the washout low. This file tracks which market contexts actually pay for that patience once costs are honest.

## Confirmed edges

_Forward-confirmed (gold): cleared the multiple-comparisons-corrected lower bound on the live forward shadow book._

_none yet_

Nothing clears the corrected lower bound on the live forward book. The strongest threads remain the bear-regime and high-volatility screens, but both live on the replay corpus only. Treat this shelf as genuinely empty until a bucket re-earns it live — and recall that the prior file once carried a forward-confirmed low-vol edge that the data itself demoted. Small-n gold is fragile; we wait for the honest forward sample.

## Screened candidates

_Replay-screened (candidate): cleared the bound on the haircut replay corpus only -- a backtest screen, NOT live-confirmed._

- **Bear-market regime (`market_trend=bear`)** — expectancy **+0.23R**, n=2311 (100 tickers), clustered 95% CI lower bound **+0.14R**, net of cost (optimistic-fill haircut applied).

  This remains the cleanest screen on the board and it lands exactly where the thesis predicts: oversold HA reversals pay best when the broad tape is scared. The mechanism is intuitive — in a bear tape genuine washouts are frequent, sellers overshoot, and the pullback entry gets filled into real capitulation rather than shallow noise. The expectancy firmed and the lower bound widened comfortably above zero on the refreshed corpus. Still backtest-only — it needs to prove out on the live forward book before it earns gold — but it is the leading promotion candidate.

- **Low score band (`score=0.00-0.50`)** — expectancy **+0.06R**, n=8440 (100 tickers), clustered 95% CI lower bound **+0.01R**, net of cost (optimistic-fill haircut applied).

  The bulk of scored setups live in this band, and it grades marginally positive with a lower bound just a hair above zero on the replay corpus. The margin is thin — read this as "the low band is not a filter to avoid" rather than the scorer separating winners. It is the base population clearing the bar by a whisker. Watch whether it survives forward.

- **High-volatility names (`volatility_tier=high`)** — expectancy **+0.25R**, n=536 (43 tickers), clustered 95% CI lower bound **+0.10R**, net of cost (optimistic-fill haircut applied).

  This holds the sign reversal from the prior file, where high-vol was a negative-expectancy hunch. On the refreshed corpus and with the confirmation-window default in place, high-vol names screen cleanly positive — the washout-into-bounce shape appears to carry further in fast names once the pause-then-confirm entry keeps us out of the worst whipsaws. The tension with the old low-vol thesis persists: both cannot be the whole story (see Open questions). Backtest screen only, and given the overlap with the bear regime, treat the two screens as possibly the same trades until the cross-tab is built.

## Hunches / needs a test

_Watched conditions that have not cleared the bound on either book -- ideas, not edges._

- **Low-volatility names (`volatility_tier=low`)** — expectancy +0.05R, n=1934 (72 tickers), clustered 95% CI lower bound **-0.06R**, net of cost. The former forward-confirmed edge, still demoted on the refreshed corpus: expectancy sits at +0.05R with the lower bound just under zero. The larger sample is the honest read — the old small-n confirmed number (+0.35R, n=113) was likely flattered. Directionally positive and worth watching, but no longer an edge and in direct tension with the high-vol screen.

- **Score 0.60–0.70 (`score=0.60-0.70`)** — expectancy +0.02R, n=152 (74 tickers), clustered 95% CI lower bound **-0.27R**, net of cost. Breakeven headline, wide negative bound. No signal yet.

- **Score 0.70–0.80 (`score=0.70-0.80`)** — expectancy +0.06R, n=39 (31 tickers), clustered 95% CI lower bound **-0.47R**, net of cost. Thin and noisy; the bound is deeply negative. Ignore until the sample grows.

- **Score 0.80–1.00 (`score=0.80-1.00`)** — expectancy -0.25R, n=9 (9 tickers), clustered 95% CI lower bound **-1.02R**, net of cost. The high-conviction band still has only nine observations, and they lost. Far too thin to conclude anything, but the scorer firing high is not yet a mark of quality.

- **Bull-market regime (`market_trend=bull`)** — expectancy -1.05R, n=1 (1 tickers), clustered 95% CI lower bound **-1.05R**, net of cost. The bull bucket has collapsed to a single observation on this corpus slice — effectively no sample. The prior file's larger read (roughly breakeven, lower bound under water) is the more informative one: reversal setups in a bull tape look like shallow dip-buys that don't develop. Treat this n=1 as noise and keep the standing intuition that this play wants a scared tape.

- **Medium-volatility names (`volatility_tier=med`)** — expectancy -1.05R, n=1 (1 tickers), clustered 95% CI lower bound **-1.05R**, net of cost. Also collapsed to n=1 here; ignore the figure. The prior larger sample sat a whisker below the bar (breakeven-plus, lower bound just missing) — no-man's-land, as before.

- **Score 0.50–0.60 (`score=0.50-0.60`)** — expectancy -1.05R, n=1 (1 tickers), clustered 95% CI lower bound **-1.05R**, net of cost. This band showed the scorer's most encouraging mid-band signal in the prior file (+0.19R, lb -0.01, n=291, nearly graduating); on this slice it is a single losing observation. Do not overwrite the prior read on n=1 — keep accumulating and re-measure.

## Falsified / retired

_Prior claims now contradicted by the evidence, kept for the record._

- **Chase entries (`reversal_ceiling_at_close`, ceiling at the trigger close)** — REFUTED 2026-07-03: fill rate jumps 44% -> 92% but confirmed-cohort expectancy collapses to **-0.062R** (clustered lower bound -0.075R, n=13,657 closed), and the rotation cohort is no better. The pullback limit's "adverse selection" is actually favorable selection — buying lower IS the edge; paying up forfeits it. The V-rotation fix is the confirmation WINDOW (catch the pause-then-confirm shape), not a chase.
- **Confirmation-bar band anchor (`reversal_anchor_confirmation`)** — REFUTED 2026-07-03: raising the band onto the bounce top lifts fills (44% -> 61%) but drops confirmed expectancy to -0.022R (lower bound -0.043R). Same lesson as the chase, in miniature.
- **Legacy reversal score (0.35 bounce / 0.20 downtrend / 0.15 volume / 0.15 confirmed / 0.15 depth)** — FALSIFIED as an ordering, 2026-07-03 rank sweep over the fixed confirmed replay book (n=9,673 closed): flat quintile ladder (+0.011 → +0.038), top-quintile-vs-rest clustered delta lower bound **-0.059**. Every individual component also failed to separate (bounce -0.060, downtrend -0.063, RSI depth -0.045, spring -0.048, volume alone -0.047); ranking calm names first actually inverted. The only ordering with a positive clustered separation bound at BOTH cost levels is **confirmation lag** (+0.030 at 0.05 slippage / +0.034 at 0.10), with flip volume additive (+0.043 / +0.048). The score was re-weighted accordingly (v2: 0.40 lag / 0.30 volume / 0.10 bounce / 0.10 downtrend / 0.10 confirmed / 0 depth, validated top-quintile bound +0.043 and the best per-day top-5 simulation of all keys tested, +0.073R lower bound +0.019). **Score-band history before 2026-07-03 measures the OLD definition** — treat the score dimension's accumulated verdicts as reset.
- **Corpus note**: the 2026-07-03 refresh (511 names, 5y through 2026-07-02, current yfinance adjustments) moved several old numbers — legacy CONFIRMED no longer clears the bound standalone (+0.004R vs the +0.125R measured 2026-06-25), and the old "confirmed does best at VIX>70" read INVERTED (vix_bucket=high -0.068R, vix_bucket=low +0.115R lower bound +0.063R on this corpus). Prior-corpus claims should be re-validated before promotion decisions lean on them.
- **Note — low-vol de-confirmation:** the prior file's forward-confirmed low-volatility edge (+0.35R, n=113) did not survive corpus expansion — it now sits at +0.05R, n=1934, lower bound -0.06R and remains in Hunches. Recorded here as a caution against small-n confirmations, not as a fully refuted idea; the low-vol thread is still live but unproven.

## Analyst calibration

_Code-owned report card on the analyst's conviction calls (scored shadow-book outcomes): does its judgment prove out?_

- **low** conviction: mean -1.02R over n=1 scored call(s).
- Nudges (final != baseline): mean -1.02R over n=1 nudged call(s).
## Open questions

- **The volatility sign flipped — which read is real?** The prior corpus said low-vol wins and high-vol loses; the refreshed corpus says high-vol screens **+0.25R (lb +0.10, n=536)** while low-vol has faded to a hunch (+0.05R, lb -0.06, n=1934). These directly contradict. Is the high-vol result driven by the confirmation-window default keeping us out of whipsaws, by the corpus refresh, or by the bear-regime overlap (high-vol names cluster on scared days)? Cross-tabulate `volatility_tier=high` against `market_trend=bear` before leaning on either.
- **Is the bear screen and the high-vol screen the same trades?** Both are the standout positive buckets and both fit "this play wants a scared tape." Cross-tab bear × high-vol to see whether one label is doing all the work.
- **Why did the bull and med-vol buckets collapse to n=1 on this slice?** These held thousands of observations in the prior file and now read as single losing observations. This looks like a corpus-slice or windowing artifact, not a real change — confirm the sampling before trusting any of the n=1 figures, and preserve the prior larger-sample reads until re-measured.
- **Prioritize forward samples for the two live screens (bear, high-vol).** Both cleared the replay bound with healthy n; the next milestone is the live forward book. Do not surface on these until they clear gold.
- The scoring model (v2: 0.40 lag / 0.30 volume) showed its best signal in the **0.50–0.60** band in the prior file (lb -0.01, nearly clearing) but that band is now n=1 on this slice, and the top bands (0.70+) remain thin and not yet predictive, with the 0.80–1.00 band actually negative on n=9. Does the scorer's high end ever earn its conviction, or is the useful signal concentrated in the mid-band once it refills?
- EARLY signals graded **+0.020R overall (clustered lower bound +0.005R)** on the prior refreshed corpus — thin but positive. The strong sub-cohorts (EARLY × vol_tier=high; EARLY × market_vol=high) came from a wide multiple-comparison sweep and align with the *new* high-vol screen's direction while contradicting the *old* low-vol confirmed edge. Now that high-vol screens positive, re-examine whether EARLY × high-vol is a coherent single edge.
- Same-day sector clustering (>=4 reversal signals in one GICS sector) tags ~85% of the book — reversal signals inherently cluster on broad red days, so the tag as defined barely discriminates. A sharper rotation definition (sector-relative return of the CLUSTER vs SPY on the flip day, or sector-ETF thrust conditioning) is the next cut worth building; the surfacing sector cap already handles the crowding symptom.

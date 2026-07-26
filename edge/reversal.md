---
forward_closed_at_last_reflection: 1055
last_reflected: 2026-07-19
---

> This playbook is maintained by the **reflection** pass (`pipeline/reflect.py`): code deterministically grades each pre-registered condition into a tiered verdict and an Opus seam authors the prose. Every change lands as a **human-gated PR** -- nothing here is auto-merged. The file is also **hand-editable**.

## Thesis

Oversold Heiken-Ashi reversal: a downtrend washout showing a green-out-of-red HA flip; enter on a pullback into the bounce, not a chase.

The core intuition is that a Heiken-Ashi color flip after a sustained downleg marks the moment sellers exhaust and the tape smooths into a bounce. The discipline is in the entry: we wait for a pullback *into* the nascent bounce rather than chasing the first green candle, so our risk is defined against the washout low. This file tracks which market contexts actually pay for that patience once costs are honest.

## Confirmed edges

_Forward-confirmed (gold): cleared the multiple-comparisons-corrected lower bound on the live forward shadow book._

_none yet_

Still nothing on the live forward book clears the corrected lower bound. The two standout replay screens — the bear regime and the high-vol bucket — remain backtest-only. Treat this shelf as genuinely empty and do not surface any bucket as gold until it re-earns the bar live. The forward book has grown to 1,055 closed observations since the last reflection (from 803); the next milestone is unchanged — watch whether the bear and high-vol screens begin clearing the corrected bound on that live sample.

## Screened candidates

_Replay-screened (candidate): cleared the bound on the haircut replay corpus only -- a backtest screen, NOT live-confirmed._

- **Bear-market regime (`market_trend=bear`)** — expectancy **+0.24R**, n=2275 (100 tickers), clustered 95% CI lower bound **+0.15R**, net of cost (optimistic-fill haircut applied).

  Still the cleanest screen on the board, and it lands exactly where the thesis predicts: oversold HA reversals pay best when the broad tape is scared. Expectancy holds at +0.24R and the lower bound firmed a touch to +0.15R across 100 tickers. Mechanism: in a bear tape genuine washouts are frequent, sellers overshoot, and the pullback entry gets filled into real capitulation rather than shallow noise. This is the leading promotion candidate — but it is backtest-only until it proves out on the live forward book.

- **Low score band (`score=0.00-0.50`)** — expectancy **+0.05R**, n=8378 (100 tickers), clustered 95% CI lower bound **+0.00R**, net of cost (optimistic-fill haircut applied).

  The bulk of scored setups live in this band, and after the v2 score re-weighting it grades marginally positive with a lower bound sitting right on zero. Read this as "the low band is not a filter to avoid" rather than a genuine edge — the base population clears the bar by a hair, no more. The bound is pinned to the line and remains fragile; it needs to survive forward before it means anything.

- **High-volatility names (`volatility_tier=high`)** — expectancy **+0.25R**, n=541 (43 tickers), clustered 95% CI lower bound **+0.10R**, net of cost (optimistic-fill haircut applied).

  Holds its positive screen (+0.25R, lower bound +0.10R) and continues to cut against the old low-vol thesis. The read remains that the washout-into-bounce shape carries further in fast names once the pause-then-confirm entry keeps us out of the worst whipsaws. The tension with the retired low-vol confirmed edge is unresolved — see Open questions, where the bear × high-vol cross-tab is still the first thing to build. Backtest screen only, and the sample is small (43 tickers) — treat with caution.

## Hunches / needs a test

_Watched conditions that have not cleared the bound on either book -- ideas, not edges._

- **Bull-market regime (`market_trend=bull`)** — expectancy +0.02R, n=5537 (100 tickers), clustered 95% CI lower bound **-0.04R**, net of cost. Where most of the sample lives, and it grades to roughly breakeven with the lower bound still under water. Reversal setups in a bull tape look like shallow dip-buys that don't develop. The contrast with the bear screen is the whole story: this play wants a scared tape.

- **Low-volatility names (`volatility_tier=low`)** — expectancy +0.03R, n=1899 (72 tickers), clustered 95% CI lower bound **-0.08R**, net of cost. The former forward-confirmed edge, still demoted and drifting slightly lower on this pass (+0.04R → +0.03R). The large sample is the honest read; the small-n confirmed number (+0.35R, n=113) was flattered. Directionally positive but no longer an edge, and in direct tension with the high-vol screen.

- **Medium-volatility names (`volatility_tier=med`)** — expectancy +0.04R, n=6427 (100 tickers), clustered 95% CI lower bound **-0.02R**, net of cost. The largest volatility bucket, sitting a whisker below the bar. Breakeven-plus with a lower bound that just misses. No-man's-land, essentially unchanged.

- **Score 0.50–0.60 (`score=0.50-0.60`)** — expectancy +0.19R, n=290 (96 tickers), clustered 95% CI lower bound **-0.03R**, net of cost. Encouraging headline expectancy but the lower bound is still under zero. Remains the closest of the score bands to graduating, and the v2 scorer's best mid-band signal. Keep accumulating.

- **Score 0.60–0.70 (`score=0.60-0.70`)** — expectancy +0.01R, n=151 (74 tickers), clustered 95% CI lower bound **-0.28R**, net of cost. Breakeven headline, wide negative bound. No signal yet.

- **Score 0.70–0.80 (`score=0.70-0.80`)** — expectancy +0.06R, n=39 (31 tickers), clustered 95% CI lower bound **-0.47R**, net of cost. Thin and noisy; the bound is deeply negative. Ignore until the sample grows.

- **Score 0.80–1.00 (`score=0.80-1.00`)** — expectancy -0.25R, n=9 (9 tickers), clustered 95% CI lower bound **-1.02R**, net of cost. The high-conviction band still has only nine observations, and they lost. Far too thin to conclude anything, but the scorer firing high is not yet a mark of quality.

## Falsified / retired

_Prior claims now contradicted by the evidence, kept for the record._

- **Chase entries (`reversal_ceiling_at_close`, ceiling at the trigger close)** — REFUTED 2026-07-03: fill rate jumps 44% -> 92% but confirmed-cohort expectancy collapses to **-0.062R** (clustered lower bound -0.075R, n=13,657 closed), and the rotation cohort is no better. The pullback limit's "adverse selection" is actually favorable selection — buying lower IS the edge; paying up forfeits it. The V-rotation fix is the confirmation WINDOW (catch the pause-then-confirm shape), not a chase.
- **Confirmation-bar band anchor (`reversal_anchor_confirmation`)** — REFUTED 2026-07-03: raising the band onto the bounce top lifts fills (44% -> 61%) but drops confirmed expectancy to -0.022R (lower bound -0.043R). Same lesson as the chase, in miniature.
- **Legacy reversal score (0.35 bounce / 0.20 downtrend / 0.15 volume / 0.15 confirmed / 0.15 depth)** — FALSIFIED as an ordering, 2026-07-03 rank sweep over the fixed confirmed replay book (n=9,673 closed): flat quintile ladder (+0.011 → +0.038), top-quintile-vs-rest clustered delta lower bound **-0.059**. Every individual component also failed to separate (bounce -0.060, downtrend -0.063, RSI depth -0.045, spring -0.048, volume alone -0.047); ranking calm names first actually inverted. The only ordering with a positive clustered separation bound at BOTH cost levels is **confirmation lag** (+0.030 at 0.05 slippage / +0.034 at 0.10), with flip volume additive (+0.043 / +0.048). The score was re-weighted accordingly (v2: 0.40 lag / 0.30 volume / 0.10 bounce / 0.10 downtrend / 0.10 confirmed / 0 depth, validated top-quintile bound +0.043 and the best per-day top-5 simulation of all keys tested, +0.073R lower bound +0.019). **Score-band history before 2026-07-03 measures the OLD definition** — treat the score dimension's accumulated verdicts as reset.
- **Corpus note**: the 2026-07-03 refresh (511 names, 5y through 2026-07-02, current yfinance adjustments) moved several old numbers — legacy CONFIRMED no longer clears the bound standalone (+0.004R vs the +0.125R measured 2026-06-25), and the old "confirmed does best at VIX>70" read INVERTED (vix_bucket=high -0.068R, vix_bucket=low +0.115R lower bound +0.063R on this corpus). Prior-corpus claims should be re-validated before promotion decisions lean on them.
- **Note — low-vol de-confirmation:** the prior file's forward-confirmed low-volatility edge (+0.35R, n=113) did not survive corpus expansion — it now sits at +0.03R, n=1899, lower bound -0.08R and remains in Hunches. Recorded here as a caution against small-n confirmations, not as a fully refuted idea; the low-vol thread is still live but unproven, and now in direct tension with the high-vol screen.
- **Sector-rotation cluster-RS tag** — RETIRED 2026-07-25 (Q4, [free-diagnostics results](../docs/plans/2026-07-25-free-diagnostics-results.md)): the sharper sector-relative definition (cluster_size>=4 AND cluster mean flip-day return − SPY >= +1.0%) DOES discriminate (tags 41.0% of the book vs the legacy count-tag's 81.5%) but carries **zero edge**: tagged-vs-untagged clustered delta +0.0004R, 95% lower bound **-0.029** (n_tagged=17,187, 501 clusters, net 0.05), and the direction also fails on the 0.10-slippage book. Pre-registered outcome: no gate, no ranking dimension — the rotation thread is closed.
- **Conviction-tier ladder (premium/strong/base) as a sizing separator** — NOT CERTIFIED 2026-07-25 (Q5, same doc): stamped premium-vs-rest delta +0.050R, Bonferroni-4 corrected clustered lower bound **-0.043**; the lag-aware challenger (confirmed AND lag>=2 AND rvol>=1.3) does no better (+0.042R, lb -0.064). Per the pre-registered rule, conviction-weighted sizing stays blocked and the tier remains a surfacing label only. Closest miss: confirmed-or-highvol vs rest (corrected lb -0.0075) — a cohort statement, not a ladder.

## Analyst calibration

_Code-owned report card on the analyst's conviction calls (scored shadow-book outcomes): does its judgment prove out?_

- **medium** conviction: mean -1.04R over n=1 scored call(s).
- **low** conviction: mean -1.03R over n=3 scored call(s).
- Nudges (final != baseline): mean -1.03R over n=3 nudged call(s).
## Open questions

- **ANSWERED 2026-07-25 — the bear × high-vol cross-tab (Q1, [free-diagnostics results](../docs/plans/2026-07-25-free-diagnostics-results.md)): BEAR is the load-bearing axis; high-vol is an amplifier *inside* bear.** On the pinned 511-name book, bear × high is the strongest cohort ever measured here (**+0.299R, clustered lb +0.218, Bonferroni-6 lb +0.187**, n=1,225, 206 clusters), bear × not-high still clears on its own (pooled lb +0.080), but bull × high FAILS (lb -0.055) — high-vol standalone was riding its bear overlap. This resolves the volatility sign-flip question: the play wants a scared tape first, fast names second. Design consequence: any premium-tier/promotion cohort should be bear-conditioned (bear × high), NOT unconditioned high-vol — an unconditioned highvol tier would surface in bull, where the cohort carries nothing. On the CONFIRMED cohort the "one edge counted twice" pattern triggers on raw bounds but misses the corrected bound (-0.003) — suggestive only.
- **Prioritize forward samples for the two live screens (bear, high-vol).** Both cleared the replay bound with healthy n and both held (or firmed) on this pass; the next milestone is the live forward book, now at 1,055 closed. Do not surface on these until they clear gold. Post-cross-tab, the cohort to watch forward is **bear × high** — and the bear axis accrues zero forward samples in a bull tape (regime-gated, not accrual-gated).
- The scoring model (v2: 0.40 lag / 0.30 volume) shows its best signal in the **0.50–0.60** band (lb -0.03, near the line) but the top bands (0.70+) are thin and not yet predictive, with the 0.80–1.00 band actually negative on n=9. Does the scorer's high end ever earn its conviction, or is the useful signal concentrated in the mid-band?
- **The first scored calls are still losing.** Three low-conviction nudges average -1.03R, and the single medium-conviction call sits at -1.04R. Too thin to mean anything yet, but the direction is now consistently negative across both conviction levels — worth watching whether the analyst's nudges systematically pull away from baseline in the wrong direction, or whether this is just small-sample noise. Revisit once n grows past single digits.
- **EARLY × high-vol — cell A PASSED 2026-07-25** (Q2, [free-diagnostics results](../docs/plans/2026-07-25-free-diagnostics-results.md)): on the pinned book the cohort grades **+0.171R, clustered lb +0.109** (n=1,940, 219 clusters, half-width 0.063 ≤ 0.10, net 0.05) — every component of the pre-registered bar, robust to single-ticker removal. EARLY need not be excluded from a high-vol/bear premium cohort. Still required before CONFIRMED-COHERENT: cell B (the rvol-gated-book read, needs sharded re-runs) and the 0.10-slippage repeats; note 42% of the cell's trades opened in 2022 — the edge is regime-concentrated, consistent with the Q1 cross-tab.
- ~~Same-day sector clustering / sharper rotation definition~~ — RESOLVED 2026-07-25: built and tested as the cluster-RS tag (Q4); discriminates but zero edge — see Falsified. The rotation thread is retired; the surfacing sector cap remains the crowding handler.

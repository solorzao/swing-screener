---
forward_closed_at_last_reflection: 2606
last_reflected: null
---

> This playbook is maintained by the **reflection** pass (`pipeline/reflect.py`): code deterministically grades each pre-registered condition into a tiered verdict and an Opus seam authors the prose. Every change lands as a **human-gated PR** -- nothing here is auto-merged. The file is also **hand-editable**.

## Thesis

Oversold Heiken-Ashi reversal: a downtrend washout showing a green-out-of-red HA flip; enter on a pullback into the bounce, not a chase.
The core intuition is that a Heiken-Ashi color flip after a sustained downleg marks the moment sellers exhaust and the tape smooths into a bounce. The discipline is in the entry: we wait for a pullback *into* the nascent bounce rather than chasing the first green candle, so our risk is defined against the washout low. This file tracks which market contexts actually pay for that patience once costs are honest.

## Confirmed edges

_Forward-confirmed (gold): cleared the multiple-comparisons-corrected lower bound on the live forward shadow book._

_none yet_

## Screened candidates

_Replay-screened (candidate): cleared the bound on the haircut replay corpus only -- a backtest screen, NOT live-confirmed._

- **market_trend=bear** (backtest screen — NOT live-confirmed): expectancy +0.30R, n=1967 (100 tickers), clustered 95% CI lower bound +0.19R, net of cost (optimistic-fill haircut applied).
- **score=0.00-0.50** (backtest screen — NOT live-confirmed): expectancy +0.05R, n=8439 (100 tickers), clustered 95% CI lower bound +0.00R, net of cost (optimistic-fill haircut applied).
- **volatility_tier=high** (backtest screen — NOT live-confirmed): expectancy +0.24R, n=561 (44 tickers), clustered 95% CI lower bound +0.09R, net of cost (optimistic-fill haircut applied).

## Hunches / needs a test

_Watched conditions that have not cleared the bound on either book -- ideas, not edges._

- market_trend=bull: expectancy +0.03R, n=5680 (100 tickers), clustered 95% CI lower bound -0.03R, net of cost (optimistic-fill haircut applied).
- score=0.50-0.60: expectancy +0.18R, n=289 (96 tickers), clustered 95% CI lower bound -0.03R, net of cost (optimistic-fill haircut applied).
- score=0.60-0.70: expectancy +0.02R, n=147 (74 tickers), clustered 95% CI lower bound -0.28R, net of cost (optimistic-fill haircut applied).
- score=0.70-0.80: expectancy +0.06R, n=40 (32 tickers), clustered 95% CI lower bound -0.46R, net of cost (optimistic-fill haircut applied).
- score=0.80-1.00: expectancy -0.28R, n=10 (10 tickers), clustered 95% CI lower bound -0.97R, net of cost (optimistic-fill haircut applied).
- volatility_tier=low: expectancy +0.02R, n=1859 (72 tickers), clustered 95% CI lower bound -0.10R, net of cost (optimistic-fill haircut applied).
- volatility_tier=med: expectancy +0.05R, n=6505 (100 tickers), clustered 95% CI lower bound -0.01R, net of cost (optimistic-fill haircut applied).

## Falsified / retired

_Prior claims now contradicted by the evidence, kept for the record._

- **Chase entries (`reversal_ceiling_at_close`, ceiling at the trigger close)** — REFUTED 2026-07-03: fill rate jumps 44% -> 92% but confirmed-cohort expectancy collapses to **-0.062R** (clustered lower bound -0.075R, n=13,657 closed), and the rotation cohort is no better. The pullback limit's "adverse selection" is actually favorable selection — buying lower IS the edge; paying up forfeits it. The V-rotation fix is the confirmation WINDOW (catch the pause-then-confirm shape), not a chase.
- **Confirmation-bar band anchor (`reversal_anchor_confirmation`)** — REFUTED 2026-07-03: raising the band onto the bounce top lifts fills (44% -> 61%) but drops confirmed expectancy to -0.022R (lower bound -0.043R). Same lesson as the chase, in miniature.
- **Legacy reversal score (0.35 bounce / 0.20 downtrend / 0.15 volume / 0.15 confirmed / 0.15 depth)** — FALSIFIED as an ordering, 2026-07-03 rank sweep over the fixed confirmed replay book (n=9,673 closed): flat quintile ladder (+0.011 → +0.038), top-quintile-vs-rest clustered delta lower bound **-0.059**. Every individual component also failed to separate (bounce -0.060, downtrend -0.063, RSI depth -0.045, spring -0.048, volume alone -0.047); ranking calm names first actually inverted. The only ordering with a positive clustered separation bound at BOTH cost levels is **confirmation lag** (+0.030 at 0.05 slippage / +0.034 at 0.10), with flip volume additive (+0.043 / +0.048). The score was re-weighted accordingly (v2: 0.40 lag / 0.30 volume / 0.10 bounce / 0.10 downtrend / 0.10 confirmed / 0 depth, validated top-quintile bound +0.043 and the best per-day top-5 simulation of all keys tested, +0.073R lower bound +0.019). **Score-band history before 2026-07-03 measures the OLD definition** — treat the score dimension's accumulated verdicts as reset.
- **Corpus note**: the 2026-07-03 refresh (511 names, 5y through 2026-07-02, current yfinance adjustments) moved several old numbers — legacy CONFIRMED no longer clears the bound standalone (+0.004R vs the +0.125R measured 2026-06-25), and the old "confirmed does best at VIX>70" read INVERTED (vix_bucket=high -0.068R, vix_bucket=low +0.115R lower bound +0.063R on this corpus). Prior-corpus claims should be re-validated before promotion decisions lean on them.
- **Note — low-vol de-confirmation:** the prior file's forward-confirmed low-volatility edge (+0.35R, n=113) did not survive corpus expansion — it now sits at +0.03R, n=1899, lower bound -0.08R and remains in Hunches. Recorded here as a caution against small-n confirmations, not as a fully refuted idea; the low-vol thread is still live but unproven, and now in direct tension with the high-vol screen.
- **Sector-rotation cluster-RS tag** — RETIRED 2026-07-25 (Q4, [free-diagnostics results](../docs/plans/2026-07-25-free-diagnostics-results.md)): the sharper sector-relative definition (cluster_size>=4 AND cluster mean flip-day return − SPY >= +1.0%) DOES discriminate (tags 41.0% of the book vs the legacy count-tag's 81.5%) but carries **zero edge**: tagged-vs-untagged clustered delta +0.0004R, 95% lower bound **-0.029** (n_tagged=17,187, 501 clusters, net 0.05), and the direction also fails on the 0.10-slippage book. Pre-registered outcome: no gate, no ranking dimension — the rotation thread is closed.
- **Wider reversal stops (`stop_buffer_atr` 0.35/0.50/0.75 vs 0.25)** — FALSIFIED 2026-07-25 (Q7, [Q6+Q7 sweep results](../docs/plans/2026-07-25-q6-q7-sweep-results.md)): despite 11.3% of winners seeing MAE ≥ 0.8R, every wider stop grades NEGATIVE vs default on the pinned book (delta points -0.0033/-0.0044/-0.0055, clustered lbs ≈ -0.028, n≈44.2k, 511 clusters). Converted stopouts become time_stops, not targets, and the drag is worst in bear (-0.008/-0.015/-0.029) — wider stops hurt most exactly where the edge lives. Per-R comparison; wider widths also carry a 1.08-1.41× bigger dollar denominator, so per-dollar it is worse still. The 0.25 default stands; no stage-2 forward variant.
- **Conviction-tier ladder (premium/strong/base) as a sizing separator** — NOT CERTIFIED 2026-07-25 (Q5, same doc): stamped premium-vs-rest delta +0.050R, Bonferroni-4 corrected clustered lower bound **-0.043**; the lag-aware challenger (confirmed AND lag>=2 AND rvol>=1.3) does no better (+0.042R, lb -0.064). Per the pre-registered rule, conviction-weighted sizing stays blocked and the tier remains a surfacing label only. Closest miss: confirmed-or-highvol vs rest (corrected lb -0.0075) — a cohort statement, not a ladder.

## Analyst calibration

_Code-owned report card on the analyst's conviction calls (scored shadow-book outcomes): does its judgment prove out?_

- **medium** conviction: mean +0.33R over n=6 scored call(s).
- **low** conviction: mean -0.14R over n=15 scored call(s).
- Nudges (final != baseline): mean -0.14R over n=15 nudged call(s).

## Open questions

_Things to investigate next._

_none yet_

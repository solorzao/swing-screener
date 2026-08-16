---
forward_closed_at_last_reflection: 1713
last_reflected: null
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

- market_trend=bear: expectancy -0.24R, n=379 (88 tickers), clustered 95% CI lower bound -0.37R, net of cost (optimistic-fill haircut applied).
- market_trend=bull: expectancy -0.13R, n=2549 (100 tickers), clustered 95% CI lower bound -0.19R, net of cost (optimistic-fill haircut applied).
- score=0.00-0.50: expectancy -0.18R, n=2203 (100 tickers), clustered 95% CI lower bound -0.23R, net of cost (optimistic-fill haircut applied).
- score=0.50-0.60: expectancy -0.11R, n=804 (100 tickers), clustered 95% CI lower bound -0.19R, net of cost (optimistic-fill haircut applied).
- score=0.60-0.70: expectancy -0.11R, n=170 (45 tickers), clustered 95% CI lower bound -0.27R, net of cost (optimistic-fill haircut applied).
- score=0.70-0.80: expectancy -0.34R, n=40 (35 tickers), clustered 95% CI lower bound -0.63R, net of cost (optimistic-fill haircut applied).
- score=0.80-1.00: expectancy -0.45R, n=23 (20 tickers), clustered 95% CI lower bound -0.82R, net of cost (optimistic-fill haircut applied).
- volatility_tier=high: expectancy -0.17R, n=80 (11 tickers), clustered 95% CI lower bound -0.42R, net of cost (optimistic-fill haircut applied).
- volatility_tier=low: expectancy -0.19R, n=1195 (79 tickers), clustered 95% CI lower bound -0.27R, net of cost (optimistic-fill haircut applied).
- volatility_tier=med: expectancy -0.14R, n=1902 (99 tickers), clustered 95% CI lower bound -0.20R, net of cost (optimistic-fill haircut applied).

## Falsified / retired

_Prior claims now contradicted by the evidence, kept for the record._

- **Re-ranking as a rescue** — FALSIFIED 2026-07-03 rank sweep (pinned corpus, n=16,219 closed, 511 clusters, book -0.161R at 0.05 slippage): no candidate ordering (legacy score, inverse score, freshness, ATR%, pullback depth/length, rvol, body, trend slope, RSI) produces a positive-expectancy top quintile; the best (`deep_pullback`, monotone -0.217 → -0.112, top-vs-rest clustered bound +0.019) still loses money. The ordering question is settled until the underlying entry economics change; the inverted-score-gradient open question is subsumed (the score separates nothing in either direction on this book).
- **Inverted score gradient as a signal** — RETIRED. The prior small sample showed 0.70+ buckets underperforming 0.50-0.60, hinting the score was anti-correlated with the edge. On the refreshed corpus the high-conviction buckets are thin and mildly-to-deeply negative with no usable structure, and the populated mass is uniformly negative. Combined with the rank sweep above, the conclusion stands: the score separates nothing in either direction. No live inversion/ablation test is warranted.
- **Breakout-confirmation timing as an edge** — `cont_confirm_window` {1,2} (fire on the first close above the flip high instead of the flip bar) improves the book from -0.161R to **-0.114R** (holds at 0.10 slippage) — the reversal late-confirm insight transfers DIRECTIONALLY, but the cohort stays decisively negative. Not an edge; kept as a default-off knob and as evidence that entry timing is a real lever on this book.
- **Thrust-denominator materiality** — the legacy volume-thrust baseline includes the pullback's own dried-up volume; the corrected denominator (`vol_thrust_excl_pullback`) changes the volband combo by ~8 trades and -0.011R (noise). Settled: keep the legacy definition. Note the volband combo itself grades negative on the refreshed corpus (its +0.07R was the prior corpus; the forward `cont_volband` book remains the arbiter).
- **Entry price (`ceiling_atr_mult`) as the last entry-economics lever** — NULL 2026-07-25 (Q6, [Q6+Q7 sweep results](../docs/plans/2026-07-25-q6-q7-sweep-results.md)): the full 0.15–0.65 sweep on the pinned corpus is perfectly monotone — tightening the ceiling genuinely improves per-trade economics (paired +0.079R at 0.15, still 93.0% fill, no fill-collapse artifact) — but the BEST cell grades **-0.103R with clustered lb -0.120** (n=15,467, 511 clusters), nowhere near zero. Entry price is a real lever; no price rescues the edge. With selection (rank sweep, med-vol gates), timing (confirm window), gates (tournament family), regime, and now entry price all falsified, the entry-economics decomposition is COMPLETE and the pre-authorized parking rule fires: continuation surfacing is to be disabled behind a `surface_continuation` flag (human-gated PR; detection/scoring/shadow-booking continue; the registered cont_volband/extguard_tight forward books remain the formal arbiters).
- **Med-vol strict-gate rescue (pullback depth + distance above a rising MA)** — NULL 2026-07-25 (Q3, [free-diagnostics results](../docs/plans/2026-07-25-free-diagnostics-results.md)): on the pinned med-vol default book (n=9,642 closed, baseline -0.150R), **zero of the 6 pre-registered cells** (ma_rising × depth ∈ {0.5,1.0} × trend_dist ∈ {1.0,2.0} + marginals, Bonferroni ×6) reaches a corrected clustered lower bound > 0 — the best cell grades -0.044R with corrected lb **-0.151** (n=396, 214 clusters), and no cell has positive point expectancy. Method note: ma_rising is degenerate (100% true — implied by the detector's trend precondition). Scope: the "shallow pullback measured tightly enough?" question is closed on the med-vol slice (~60% of the closed book), not book-wide. This was the last selection-side open question; the remaining lever is entry economics (the pre-registered `ceiling_atr_mult` sweep, queue Q6).

## Analyst calibration

_Code-owned report card on the analyst's conviction calls (scored shadow-book outcomes): does its judgment prove out?_

- **medium** conviction: mean -0.73R over n=8 scored call(s).
- **low** conviction: mean -0.32R over n=35 scored call(s).
- Nudges (final != baseline): mean -0.32R over n=35 nudged call(s).

## Open questions

_Things to investigate next._

_none yet_

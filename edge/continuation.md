---
forward_closed_at_last_reflection: 851
last_reflected: 2026-07-16
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
Read the expectancies as "where the mass currently sits," not as a signal to trade. With
another refresh of the corpus the story has not moved: the central estimates remain a
mild-but-persistent negative, and the intervals keep closing on that same negative
number rather than widening back toward hope.

**Market regime.** The core continuation thesis is meant to live in a bull tape, and
that slice is the deepest in the file -- and still under water:

- **market_trend=bull**: expectancy -0.14R, n=2476 (100 tickers), clustered 95% CI
  lower bound -0.20R, net of cost. This is the whole thesis in one line: with the
  largest sample we have, the central estimate is negative and the upper reach of the
  interval no longer touches a tradable number. The single most important warning sign
  in the file.
- **market_trend=bear**: expectancy -0.25R, n=401 (88 tickers), clustered 95% CI lower
  bound -0.36R, net of cost. The worst regime, as a pullback-continuation long in a
  downtrend should be. The bear bucket admits samples, and they lose -- exactly what a
  trend-following long should do when the tape is against it.

**Score buckets** (the model's own conviction) still fail to sort outcomes in the
direction we'd want -- every populated bucket is negative, and the two highest buckets
that do carry mass here grade *worst*:

- **score=0.00-0.50**: expectancy -0.18R, n=2194 (100 tickers), clustered 95% CI lower
  bound -0.23R, net of cost.
- **score=0.50-0.60**: expectancy -0.11R, n=788 (100 tickers), clustered 95% CI lower
  bound -0.19R, net of cost. The least-bad populated bucket, but its lower bound is
  still under water.
- **score=0.60-0.70**: expectancy -0.11R, n=165 (45 tickers), clustered 95% CI lower
  bound -0.28R, net of cost.
- **score=0.70-0.80**: expectancy -0.50R, n=16 (15 tickers), clustered 95% CI lower
  bound -0.88R, net of cost. Thin (16 trades) and deeply negative.
- **score=0.80-1.00**: expectancy -0.60R, n=5 (5 tickers), clustered 95% CI lower bound
  -0.96R, net of cost. Five trades, all-but-uninterpretable on its own, but pointing the
  wrong way.

The top buckets, which were empty on the prior corpus, have now filled with a handful of
losing trades. Do not over-read n=16 and n=5 -- but note they do nothing to rehabilitate
the score as a selector, and everything to confirm the falsified rank sweep: the score
separates nothing in the direction we'd want.

**Volatility tiers** all sit negative, converged toward the same mild negative as the
sample has grown:

- **volatility_tier=high**: expectancy -0.14R, n=77 (11 tickers), clustered 95% CI lower
  bound -0.40R, net of cost. On a thin 11-ticker sample with a wide interval -- don't
  over-read either the level or its movement.
- **volatility_tier=med**: expectancy -0.14R, n=1879 (99 tickers), clustered 95% CI
  lower bound -0.20R, net of cost. The deepest tier and the tightest interval; a clean,
  survivable-looking central estimate that nonetheless does not clear the bound.
- **volatility_tier=low**: expectancy -0.19R, n=1191 (79 tickers), clustered 95% CI
  lower bound -0.26R, net of cost.

## Falsified / retired

_Prior claims now contradicted by the evidence, kept for the record._

- **Re-ranking as a rescue** — FALSIFIED 2026-07-03 rank sweep (pinned corpus, n=16,219 closed, 511 clusters, book -0.161R at 0.05 slippage): no candidate ordering (legacy score, inverse score, freshness, ATR%, pullback depth/length, rvol, body, trend slope, RSI) produces a positive-expectancy top quintile; the best (`deep_pullback`, monotone -0.217 → -0.112, top-vs-rest clustered bound +0.019) still loses money. The ordering question is settled until the underlying entry economics change; the inverted-score-gradient open question is subsumed (the score separates nothing in either direction on this book).
- **Inverted score gradient as a signal** — RETIRED. The prior small sample showed 0.70+ buckets underperforming 0.50-0.60, hinting the score was anti-correlated with the edge. On the refreshed corpus the high-conviction buckets are thin and mildly-to-deeply negative with no usable structure, and the populated mass is uniformly negative. Combined with the rank sweep above, the conclusion stands: the score separates nothing in either direction. No live inversion/ablation test is warranted.
- **Breakout-confirmation timing as an edge** — `cont_confirm_window` {1,2} (fire on the first close above the flip high instead of the flip bar) improves the book from -0.161R to **-0.114R** (holds at 0.10 slippage) — the reversal late-confirm insight transfers DIRECTIONALLY, but the cohort stays decisively negative. Not an edge; kept as a default-off knob and as evidence that entry timing is a real lever on this book.
- **Thrust-denominator materiality** — the legacy volume-thrust baseline includes the pullback's own dried-up volume; the corrected denominator (`vol_thrust_excl_pullback`) changes the volband combo by ~8 trades and -0.011R (noise). Settled: keep the legacy definition. Note the volband combo itself grades negative on the refreshed corpus (its +0.07R was the prior corpus; the forward `cont_volband` book remains the arbiter).

## Analyst calibration

_Code-owned report card on the analyst's conviction calls (scored shadow-book outcomes): does its judgment prove out?_

- **medium** conviction: mean -0.57R over n=3 scored call(s).
- **low** conviction: mean -0.08R over n=8 scored call(s).
- Nudges (final != baseline): mean -0.08R over n=8 nudged call(s).
## Open questions

_Things to investigate next._

- **Is entry timing the one real lever?** The falsified section shows late
  breakout-confirmation (`cont_confirm_window`) moves the pinned book from -0.161R to
  -0.114R. That's directional but not enough. Given every slice here sits mildly
  negative, the surviving hypothesis is that the *entry economics* (fill point, initial
  stop placement) — not selection — keep the book under breakeven. Pre-register a sweep
  of entry-trigger and stop rules rather than more candidate re-rankings.
- **Does the med-vol tier alone approach breakeven with tighter gates?** volatility_tier=med
  is the deepest, tightest slice (-0.14R, n=1879, lower bound -0.20R). Test whether a
  strict continuation filter (pullback depth + distance above a rising MA) applied to the
  med-vol cut lifts it toward the bound, or whether the weakness is structural.
- **Is "shallow pullback" measured tightly enough?** Still open. Consider pullback-depth
  and trend-strength (distance above a rising MA) as pre-registered gates before the HA
  flip, to see whether a stricter continuation filter separates winners from losers.
- **Why does the bull slice — the thesis's home regime — stay negative at scale?** With
  n=2476 and a lower bound of -0.20R, the core continuation idea is not merely
  unconfirmed but leaning negative. Before iterating on filters, confirm the signal
  definition (HA flip out of a shallow pause) is firing where we think it is, and not
  systematically entering after the trend leg has already exhausted.
- **Do the analyst's medium-conviction calls have a common failure mode?** With n=3 they
  are the worst-scored tier. Once the count grows, inspect whether the medium calls
  cluster in a particular regime or score band — or whether the overrides simply track
  the book's uniform negativity.

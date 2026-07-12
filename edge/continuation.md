---
forward_closed_at_last_reflection: 709
last_reflected: 2026-07-12
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

Note the striking change since the last reflection: the large, deep bull-tape slice
(previously n=2442) has essentially drained out of this rendering, and what remains for
the market-regime and volatility cuts is thin, high-variance rubble. Treat the small-n
lines here with extreme suspicion — a lone winning trade can flatter a whole tier.

**Market regime.** The core continuation thesis is meant to live in a bull tape, but on
this rendering the bull slice has collapsed to a handful of trades and the bear slice
now carries most of the regime mass:

- **market_trend=bear**: expectancy -0.25R, n=403 (88 tickers), clustered 95% CI lower
  bound -0.37R, net of cost. The best-populated regime line, and it loses — as a
  pullback-continuation long taken into a downtrend should. The bear bucket admits
  samples and they bleed.
- **market_trend=bull**: expectancy -0.54R, n=15 (15 tickers), clustered 95% CI lower
  bound -0.95R, net of cost. This is the thesis's home regime and it is now nearly
  empty and deeply negative on the trades that remain. Do not read -0.54R as a stable
  estimate — n=15 across 15 tickers is one trade per name and the interval is enormous.
  But there is nothing here to like.

**Score buckets** (the model's own conviction) still fail to sort outcomes — every
populated bucket is negative and the high-conviction tail is empty:

- **score=0.00-0.50**: expectancy -0.17R, n=2188 (100 tickers), clustered 95% CI lower
  bound -0.22R, net of cost. The deepest, tightest bucket in the file, and it sits
  firmly under water.
- **score=0.50-0.60**: expectancy -0.10R, n=787 (100 tickers), clustered 95% CI lower
  bound -0.19R, net of cost. The least-bad well-populated bucket, but its lower bound is
  still negative.
- **score=0.60-0.70**: expectancy -1.03R, n=1 (1 tickers), clustered 95% CI lower bound
  -1.03R, net of cost. A single trade — a data point, not a distribution. Ignore the
  headline number.
- **score=0.70-0.80**: expectancy -0.50R, n=14 (14 tickers), clustered 95% CI lower
  bound -0.94R, net of cost. Thin and negative.
- **score=0.80-1.00**: expectancy +0.00R, n=0 (0 tickers), clustered 95% CI lower bound
  +0.00R, net of cost. Empty — nothing to say.

The score continues to separate nothing in the direction we'd want: the well-populated
low buckets are negative and the sparse high buckets are either negative or empty. This
is consistent with the falsified rank sweep — the score is not a usable sort key on this
book.

**Volatility tiers** are all thin on this rendering and dominated by variance:

- **volatility_tier=high**: expectancy +1.47R, n=1 (1 tickers), clustered 95% CI lower
  bound +1.47R, net of cost. A single winning trade. This is the one positive line in
  the file and it is worth exactly nothing as evidence — n=1 with a degenerate interval.
  Do not chase it.
- **volatility_tier=med**: expectancy -0.68R, n=9 (9 tickers), clustered 95% CI lower
  bound -0.93R, net of cost. Thin and negative.
- **volatility_tier=low**: expectancy -0.69R, n=5 (5 tickers), clustered 95% CI lower
  bound -1.07R, net of cost. Thin and negative.

The prior reflection's clean, deep volatility tiers (med at n=1858) have not survived
into this rendering; what's here is too thin to lean on in either direction.

## Falsified / retired

_Prior claims now contradicted by the evidence, kept for the record._

- **Re-ranking as a rescue** — FALSIFIED 2026-07-03 rank sweep (pinned corpus, n=16,219 closed, 511 clusters, book -0.161R at 0.05 slippage): no candidate ordering (legacy score, inverse score, freshness, ATR%, pullback depth/length, rvol, body, trend slope, RSI) produces a positive-expectancy top quintile; the best (`deep_pullback`, monotone -0.217 → -0.112, top-vs-rest clustered bound +0.019) still loses money. The ordering question is settled until the underlying entry economics change; the inverted-score-gradient open question is subsumed (the score separates nothing in either direction on this book).
- **Inverted score gradient as a signal** — RETIRED. The prior small sample showed 0.70+ buckets underperforming 0.50-0.60, hinting the score was anti-correlated with the edge. On the refreshed corpus the high-conviction buckets are empty or single-trade (0.60-0.70 n=1, 0.70-0.80 n=14, 0.80-1.00 n=0) and the populated buckets are all mildly negative with no monotone structure. Combined with the rank sweep above, the conclusion stands: the score separates nothing in either direction. No live inversion/ablation test is warranted.
- **Breakout-confirmation timing as an edge** — `cont_confirm_window` {1,2} (fire on the first close above the flip high instead of the flip bar) improves the book from -0.161R to **-0.114R** (holds at 0.10 slippage) — the reversal late-confirm insight transfers DIRECTIONALLY, but the cohort stays decisively negative. Not an edge; kept as a default-off knob and as evidence that entry timing is a real lever on this book.
- **Thrust-denominator materiality** — the legacy volume-thrust baseline includes the pullback's own dried-up volume; the corrected denominator (`vol_thrust_excl_pullback`) changes the volband combo by ~8 trades and -0.011R (noise). Settled: keep the legacy definition. Note the volband combo itself grades -0.09R on the refreshed corpus (its +0.07R was the prior corpus; the forward `cont_volband` book remains the arbiter).

## Analyst calibration

_Code-owned report card on the analyst's conviction calls (scored shadow-book outcomes): does its judgment prove out?_

- **low** conviction: mean -0.22R over n=5 scored call(s).
- Nudges (final != baseline): mean -0.22R over n=5 nudged call(s).
## Open questions

_Things to investigate next._

- **Why did the deep bull/med-vol slices drain from this rendering?** The prior
  reflection leaned on a bull slice of n=2442 and a med-vol slice of n=1858; this
  rendering shows n=15 and n=9 for those cuts. Before drawing any regime conclusion,
  confirm whether this is a corpus/window change or a signal-definition change — the
  thin lines here cannot bear the weight the prior deep ones did.
- **Is entry timing the one real lever?** The falsified section shows late
  breakout-confirmation (`cont_confirm_window`) moves the pinned book from -0.161R to
  -0.114R. That's directional but not enough. The surviving hypothesis is that entry
  economics (fill point, initial stop placement) — not selection — keep the book under
  breakeven. Pre-register a sweep of entry-trigger and stop rules rather than more
  candidate re-rankings.
- **Does any well-populated slice approach breakeven with tighter gates?** The two deep
  buckets that remain — score=0.00-0.50 (n=2188, lower bound -0.22R) and score=0.50-0.60
  (n=787, lower bound -0.19R) — are the only lines with enough mass to test. Ask whether
  a strict continuation filter (pullback depth + distance above a rising MA) lifts either
  toward the bound, or whether the weakness is structural.
- **Is "shallow pullback" measured tightly enough?** Still open. Consider pullback-depth
  and trend-strength (distance above a rising MA) as pre-registered gates before the HA
  flip, to see whether a stricter continuation filter separates winners from losers.
- **Why does the bear slice carry the regime mass?** With the bull slice down to n=15
  and bear at n=403, most of what's being logged are continuation-longs into downtrends
  — precisely where the thesis should not fire. Confirm the uptrend precondition is
  actually gating entries, and not letting bear-tape flips through.

---
forward_closed_at_last_reflection: 956
last_reflected: 2026-07-19
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

- **market_trend=bull**: expectancy -0.14R, n=2484 (100 tickers), clustered 95% CI
  lower bound -0.20R, net of cost. This is the whole thesis in one line: with the
  largest sample we have, the central estimate is negative and the upper reach of the
  interval no longer touches a tradable number. The single most important warning sign
  in the file. The added trades since last reflection did nothing to bend it upward.
- **market_trend=bear**: expectancy -0.24R, n=396 (88 tickers), clustered 95% CI lower
  bound -0.36R, net of cost. The worst regime, as a pullback-continuation long in a
  downtrend should be. The bear bucket admits samples, and they lose -- exactly what a
  trend-following long should do when the tape is against it.

**Score buckets** (the model's own conviction) still fail to sort outcomes in the
direction we'd want -- every populated bucket is negative, and the two highest buckets
that carry any mass here grade *worst*:

- **score=0.00-0.50**: expectancy -0.17R, n=2195 (100 tickers), clustered 95% CI lower
  bound -0.23R, net of cost.
- **score=0.50-0.60**: expectancy -0.11R, n=789 (100 tickers), clustered 95% CI lower
  bound -0.19R, net of cost. The least-bad populated bucket, but its lower bound is
  still under water.
- **score=0.60-0.70**: expectancy -0.12R, n=168 (45 tickers), clustered 95% CI lower
  bound -0.28R, net of cost.
- **score=0.70-0.80**: expectancy -0.55R, n=20 (18 tickers), clustered 95% CI lower
  bound -0.87R, net of cost. Thin (20 trades) and deeply negative.
- **score=0.80-1.00**: expectancy -0.66R, n=11 (8 tickers), clustered 95% CI lower bound
  -0.99R, net of cost. Eleven trades, all-but-uninterpretable on its own, but pointing
  the wrong way.

The top buckets have filled a little more since last reflection and are, if anything,
sinking rather than rehabilitating. Do not over-read n=20 and n=11 -- but note they do
nothing to rescue the score as a selector, and everything to confirm the falsified rank
sweep: the score separates nothing in the direction we'd want.

**Volatility tiers** all sit negative, converged toward the same mild negative as the
sample has grown:

- **volatility_tier=high**: expectancy -0.16R, n=79 (11 tickers), clustered 95% CI lower
  bound -0.42R, net of cost. On a thin 11-ticker sample with a wide interval -- don't
  over-read either the level or its movement.
- **volatility_tier=med**: expectancy -0.13R, n=1882 (99 tickers), clustered 95% CI
  lower bound -0.20R, net of cost. The deepest tier and the tightest interval; a clean,
  survivable-looking central estimate that nonetheless does not clear the bound.
- **volatility_tier=low**: expectancy -0.19R, n=1191 (79 tickers), clustered 95% CI lower
  bound -0.26R, net of cost.

## Falsified / retired

_Prior claims now contradicted by the evidence, kept for the record._

- **Re-ranking as a rescue** — FALSIFIED 2026-07-03 rank sweep (pinned corpus, n=16,219 closed, 511 clusters, book -0.161R at 0.05 slippage): no candidate ordering (legacy score, inverse score, freshness, ATR%, pullback depth/length, rvol, body, trend slope, RSI) produces a positive-expectancy top quintile; the best (`deep_pullback`, monotone -0.217 → -0.112, top-vs-rest clustered bound +0.019) still loses money. The ordering question is settled until the underlying entry economics change; the inverted-score-gradient open question is subsumed (the score separates nothing in either direction on this book).
- **Inverted score gradient as a signal** — RETIRED. The prior small sample showed 0.70+ buckets underperforming 0.50-0.60, hinting the score was anti-correlated with the edge. On the refreshed corpus the high-conviction buckets are thin and mildly-to-deeply negative with no usable structure, and the populated mass is uniformly negative. Combined with the rank sweep above, the conclusion stands: the score separates nothing in either direction. No live inversion/ablation test is warranted.
- **Breakout-confirmation timing as an edge** — `cont_confirm_window` {1,2} (fire on the first close above the flip high instead of the flip bar) improves the book from -0.161R to **-0.114R** (holds at 0.10 slippage) — the reversal late-confirm insight transfers DIRECTIONALLY, but the cohort stays decisively negative. Not an edge; kept as a default-off knob and as evidence that entry timing is a real lever on this book.
- **Thrust-denominator materiality** — the legacy volume-thrust baseline includes the pullback's own dried-up volume; the corrected denominator (`vol_thrust_excl_pullback`) changes the volband combo by ~8 trades and -0.011R (noise). Settled: keep the legacy definition. Note the volband combo itself grades negative on the refreshed corpus (its +0.07R was the prior corpus; the forward `cont_volband` book remains the arbiter).
- **Entry price (`ceiling_atr_mult`) as the last entry-economics lever** — NULL 2026-07-25 (Q6, [Q6+Q7 sweep results](../docs/plans/2026-07-25-q6-q7-sweep-results.md)): the full 0.15–0.65 sweep on the pinned corpus is perfectly monotone — tightening the ceiling genuinely improves per-trade economics (paired +0.079R at 0.15, still 93.0% fill, no fill-collapse artifact) — but the BEST cell grades **-0.103R with clustered lb -0.120** (n=15,467, 511 clusters), nowhere near zero. Entry price is a real lever; no price rescues the edge. With selection (rank sweep, med-vol gates), timing (confirm window), gates (tournament family), regime, and now entry price all falsified, the entry-economics decomposition is COMPLETE and the pre-authorized parking rule fires: continuation surfacing is to be disabled behind a `surface_continuation` flag (human-gated PR; detection/scoring/shadow-booking continue; the registered cont_volband/extguard_tight forward books remain the formal arbiters).
- **Retest-limit entry (NEGATIVE `ceiling_atr_mult`) — the parking rule's named re-entry path** — NULL 2026-08-16 (Q8, [pre-registration](../docs/plans/2026-08-16-retest-entry-preregistration.md) · [results](../docs/plans/2026-08-16-retest-entry-results.md)). Q6 settled the *chase* family only: `ceiling = trigger_close + mult × ATR`, so all six of its cells were limits ABOVE the close and the sweep varied only how hard we chase the flip bar. A NEGATIVE mult inverts the mechanic — the limit sits below the close, the bar must retrace into it to fill — moving PRICE (lower entry, unchanged stop → smaller `risk` → the same move books as a larger R) and SELECTION (only setups that pull back are taken) at once. Both gates passed (anchor reproduces the pinned book at -0.1613R / n=16,221 / 511 clusters / 97.4% fill and is 100% row-identical to `D_dump`; all six cells share a byte-identical reversal book). The sweep is monotone and by far the largest lever ever measured on this book — **-0.161R → -0.015R, ~91% of the gap to breakeven** (0.00: -0.060R lb -0.078; -0.15: -0.035R lb -0.055; -0.30: -0.030R lb -0.052; **-0.50: -0.015R lb -0.044**, n=6,368, 40.8% fill; -0.75: -0.022R lb -0.065) — beating timing (-0.114R) and the above-market ceiling sweep (-0.103R). The mechanism is confirmed, not incidental: avg `risk` 9.05 → 4.88, target exits 14.3% → 27.0%, momentum-flip exits 47.9% → 29.3%, holds 4.4 → 2.2 bars. **It still never clears zero.** This is a DECISIVE null, not a thin one: every cell is decisional (half-widths 0.017–0.043R against the 0.10R bar) and the best cell carries 6,368 trades over 511 clusters, so it is *not* the "expectancy wins while never filling" artifact — the pre-registration named that failure mode in advance and it did not occur. The grid is CLOSED: no extension, no re-cut on the near miss (-0.015R is not breakeven; it is -0.015R with a lower bound of -0.044R), and -0.75 turning back down puts an interior optimum near -0.50 rather than a curve still climbing. Read as an UPPER bound: OHLC replay fills a retest at `min(bar_high, ceiling)` and cannot see queue position or adverse selection. Not chasing fixes most of what is broken with this entry — and most is not enough.
- **Med-vol strict-gate rescue (pullback depth + distance above a rising MA)** — NULL 2026-07-25 (Q3, [free-diagnostics results](../docs/plans/2026-07-25-free-diagnostics-results.md)): on the pinned med-vol default book (n=9,642 closed, baseline -0.150R), **zero of the 6 pre-registered cells** (ma_rising × depth ∈ {0.5,1.0} × trend_dist ∈ {1.0,2.0} + marginals, Bonferroni ×6) reaches a corrected clustered lower bound > 0 — the best cell grades -0.044R with corrected lb **-0.151** (n=396, 214 clusters), and no cell has positive point expectancy. Method note: ma_rising is degenerate (100% true — implied by the detector's trend precondition). Scope: the "shallow pullback measured tightly enough?" question is closed on the med-vol slice (~60% of the closed book), not book-wide. This was the last selection-side open question; the remaining lever is entry economics (the pre-registered `ceiling_atr_mult` sweep, queue Q6).

## Analyst calibration

_Code-owned report card on the analyst's conviction calls (scored shadow-book outcomes): does its judgment prove out?_

- **medium** conviction: mean -0.68R over n=5 scored call(s).
- **low** conviction: mean -0.18R over n=9 scored call(s).
- Nudges (final != baseline): mean -0.18R over n=9 nudged call(s).
## Open questions

_Things to investigate next._

- ~~Is entry timing the one real lever?~~ — **CLOSED 2026-07-25** (Q6): the pre-registered
  `ceiling_atr_mult` sweep answered the entry-economics question — the lever is real
  (monotone, +0.079R paired at the tightest ceiling) but insufficient (best cell
  -0.103R, lb -0.120; see Falsified). Every decomposition axis is now falsified; the
  parking rule is active. No further continuation experiments without a fundamentally
  different entry mechanic.
- ~~Does a RETEST-limit entry — the parking rule's named re-entry path — rescue the
  book?~~ — **CLOSED as a null 2026-08-16** (Q8): a negative `ceiling_atr_mult` bids
  BELOW the trigger close instead of chasing it, which Q6 never tested. It is the
  largest lever ever measured here (-0.161R → -0.015R, ~91% of the gap) and still never
  clears zero (best lb -0.044R); decisive, not thin; grid closed (see Falsified). This
  retires the specific mechanic the parking rule named. Continuation remains parked, and
  a future proposal must be a genuinely different SETUP or exit — not another entry-price
  point, which is now falsified in both directions.
- ~~Does the med-vol tier alone approach breakeven with tighter gates? / Is "shallow
  pullback" measured tightly enough?~~ — **CLOSED as a null 2026-07-25** (Q3): the strict
  med-vol cut was tested with a pre-registered 6-cell grid and zero cells cleared the
  corrected bound (best -0.044R, corrected lb -0.151; see Falsified). The weakness is
  structural, not a filtering miss. The shallow-pullback closure carries a med-vol-slice
  scope caveat. No further continuation selection-side experiments without new
  entry-economics evidence; the one remaining pre-registered lever is the `ceiling_atr_mult`
  sweep (queue Q6).
- **Why does the bull slice — the thesis's home regime — stay negative at scale?** With
  n=2484 and a lower bound of -0.20R, the core continuation idea is not merely
  unconfirmed but leaning negative. Before iterating on filters, confirm the signal
  definition (HA flip out of a shallow pause) is firing where we think it is, and not
  systematically entering after the trend leg has already exhausted.
- **Do the analyst's medium-conviction calls have a common failure mode?** Now n=5 and
  still the worst-scored tier at -0.68R. Inspect whether the medium calls cluster in a
  particular regime or score band — or whether the overrides simply track the book's
  uniform negativity.

# Reversal score overhaul — the ordering behind the top-5 was noise; confirmation lag is signal

**Trigger.** The reversal score is the only ordering behind the digest's top-5 picks, yet
`edge/reversal.md` graded it non-predictive, and during the Jul 1–2 rotation CRM/WDAY/PTC
ranked 9–12 by score and lost the top-5 race. Follow-up to
`docs/plans/2026-07-03-reversal-rotation-capture.md` (PR #87).

## Method — the score never gates, so ranking is a pure post-hoc question

The trade book is invariant to score weights. So instead of replay walks per weighting, one
pass recomputes the detector context at every booked confirmed trade's trigger bar
(`scripts/replay_rank_sweep.py`, using the PR #87 tournament books: confirm3 variant,
511 names / 5y, slip 0.05 and 0.10) and evaluates candidate orderings offline:

1. **Quintile ladder** of realized R over closed fills (is the key monotone?);
2. **Top-quintile vs rest**, ticker-clustered two-sample delta lower bound (the separation
   test);
3. **Digest simulation**: per `opened_date`, top-5 by key — selected-cohort expectancy,
   per-pick value (missed picks count 0R), fill rate.

## Results (confirmed cohort, n=22,672 signals / 9,673 closed, slip 0.05; 0.10 in parens)

| ordering key | ladder q1→q5 | top-quintile vs rest, clustered delta low |
|---|---|---|
| legacy score | +0.011 → +0.038 (humped) | **-0.059** (-0.050) |
| flip volume alone | -0.026 → +0.028 | -0.047 (-0.042) |
| bounce quality / downtrend / RSI depth / spring | flat or humped | -0.041 … -0.063 |
| calm-first (low ATR%) | **inverted** (q1/q2 best) | -0.080 (-0.084) |
| **confirmation lag (late-first)** | -0.034 → +0.114 monotone | **+0.030 (+0.034)** |
| **lag + volume** | -0.034 → +0.119 monotone | **+0.043 (+0.048)** |

The lag result is the ranking-space echo of PR #87's late-confirm increment edge
(+0.110R, lb +0.065): flips that pause before confirming out-perform one-bar rips, and the
current book's worst cohort is exactly the lag-1 vertical confirmations the legacy score
tended to favor (big body, big volume, deep RSI).

## Shipped

- **Score weights lifted into `StrategyConfig`** (`reversal_score_w_*`, normalized by their
  sum so only ratios matter) — the scorer is finally sweepable by the replay variant
  machinery; the legacy vector remains exactly reproducible via explicit weights.
- **`ReversalContext.confirm_lag`** (bars from flip to confirmation; 0 for EARLY) carried
  from the detector and fed to the scorer.
- **Score v2 default weights: 0.40 lag / 0.30 volume / 0.10 bounce / 0.10 downtrend /
  0.10 confirmed / 0 depth.** Validated on the same book before shipping: top-quintile
  +0.097R (own bound +0.043), q5-vs-rest bound +0.011 (holds at 0.10: +0.017), and the
  best per-day top-5 simulation of every key tested (+0.073R, bound +0.019, vs legacy's
  +0.045 / -0.009). Depth is 0 because RSI depth showed no separation (still sweepable).
- Acceptance replay: the Jul 1 digest now leads with **MSFT** (a late confirm the legacy
  rule classified "already ran 3+ green") and carries **PLTR** — software surfaced on the
  thrust day. Jul 2 ranks late confirms above the lag-1 CRM/WDAY (which stay on the
  overflow line) — that preference is the measured edge, not a bug.

## Caveats

- All ranking evidence is conditioned on the book's fill mechanics (pullback limits) and
  the ticker-clustered bootstrap ignores time clustering; the score dimension's
  pre-2026-07-03 band history in `edge/reversal.md` measures the OLD score definition and
  is reset. Forward re-grading accrues via the normal reflection loop.
- The digest simulation is a weak discriminator on quiet days (pool <= 5 selects
  everything); the quintile separation test is the load-bearing evidence.

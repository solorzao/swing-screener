# Queued-experiment batch — target geometry wins; continuation ordering falsified

Runs the 2026-07-03 review's experiment queue on the PINNED corpus (511 names / 5y,
cache vintage `as-of 20260703`), sharded via `scripts/replay_queue_experiments.py`,
net of ATR slippage with ticker-clustered bounds. Companion tooling shipped in PR #93.

## 1. Reversal target geometry — the winner (shipped)

Same-sample sweep (identical entries, only `reversal_retrace_frac` differs; ~44k closed
fills per arm):

| retrace frac | full book @0.05 exp / corrected low | confirmed @0.05 | confirmed @0.10 |
|---|---|---|---|
| 0.50 | -0.027 / -0.039 | -0.027 / -0.049 | -0.065 / -0.087 |
| 0.618 | -0.001 / -0.016 | +0.007 / -0.017 | -0.029 / -0.053 |
| 0.786 (old default) | +0.024 / +0.009 | +0.039 / +0.012 | +0.006 / -0.021 |
| **1.00** | **+0.043 / +0.027** | **+0.057 / +0.029** | +0.027 / -0.002 |

Monotone in both cohorts at both cost levels: the 78.6% retrace capped winners early.
**Shipped:** default `reversal_retrace_frac` 0.786 → 1.0 (target = the full breakdown
level); legacy 0.786 forward-tracked as `rev_retrace786`. The 0.10 bound (-0.002) is the
same fragility profile the confirmed+no-flip edge shipped with; the forward A/B decides.

## 2. Continuation rank sweep — ordering falsified

On the fixed default book (-0.161R, n=16,219 closed, 511 clusters, 97% fill) no
candidate ordering produces a positive-expectancy top quintile; the only separator is
`deep_pullback` (monotone -0.217 → -0.112, top-vs-rest clustered bound +0.019) and even
its best quintile loses. Re-ranking cannot rescue this book; the problem is entry
economics. Method note: `mtf_aligned` / `timeframe` cohorts are UNMEASURABLE on replay
books (the walk is single-timeframe, so the stamp is degenerate there) — those reads
must come from the forward book's `would_surface` facet.

## 3. Continuation confirm window — directional, not an edge

`cont_confirm_window` {1, 2} (fire on the first close above the flip high): the book
improves from -0.161R to **-0.114R / -0.116R** (corrected lows ~-0.14; holds at 0.10).
The reversal late-confirm insight transfers directionally — later, confirmed entries
lose less — but the cohort stays decisively negative. Recorded as falsified-as-edge;
the knob stays in the codebase default-off as a proven lever for any future combination.

## 4. Thrust denominator — settled, immaterial

`vol_thrust_excl_pullback` (honest baseline excluding the pullback's dried-up volume)
changes the volband combo by ~8 trades and -0.011R — noise. Keep the legacy definition.
Corpus note: the volband combo itself grades -0.09R here (its +0.07R claim was the
prior corpus); the forward `cont_volband` book remains the arbiter.

## 5. Forward instrumentation now accruing (PR #93)

The `be_1r` breakeven arm books from the next screen run and is judged by
`paired_arm_delta` (the first honest same-sample arm judge); `low_water` MAE accrues on
every advanced trade, making stop-width / breakeven-timing / target-reachability
questions answerable offline from here on.

**Update 2026-07-12:** the experiment settle bar was tightened 0.15R → 0.10R (reviewed
seed value) — see [2026-07-12-settle-bar-recalibration.md](2026-07-12-settle-bar-recalibration.md),
which also carries the forward-accrual watch item (checkpoint ≈ 2026-09, escape hatch 0.125R).

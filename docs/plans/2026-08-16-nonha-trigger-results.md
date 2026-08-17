# Q9 cont_nonha_trigger -- raw-candle triggers vs the HA flip

Pinned corpus as-of 20260703; slippage 0.05 ATR baked into realized_r; house ticker-clustered bootstrap (seeded, canonical order); continuation cohort only. Pre-registration: docs/plans/2026-08-16-nonha-trigger-preregistration.md

- loaded `X_trig_a`: 8 shards, 266737 rows, variants ['default', 'raw_reclaim', 'raw_up']
- loaded `X_trig_b`: 8 shards, 81053 rows, variants ['raw_reclaim_hl']
- loaded `D_dump`: 8 shards, 87090 rows, variants ['default']

## 1. Sanity anchor (gate)

X_trig_a `default` continuation book: n_signals=16813, filled=16273, missed=426, fill%=97.4%, n_closed=16221, clusters=511, expectancy=-0.1613R, clustered lb95=-0.1782R
- PASS: expectancy within +/-0.005 of -0.161R (-0.1613R)
- PASS: n_closed in [15900, 16500] (16221)
- PASS: clusters >= 500 (511)
- PASS: fill% in [95%, 99%] (97.4%)

Cross-walk identity vs D_dump default continuation: 16813/16813 keys matched (100.00%); only-in-sweep=0, only-in-D_dump=0
- per-column mismatches: {'entry_price': 0, 'stop': 0, 'target': 0, 'risk': 0, 'realized_r': 0, 'hold_bars': 0, 'fill_status': 0, 'status': 0, 'exit_reason': 0, 'exit_date': 0}
- PASS: trade sets identical

**Anchor PASS** - the walk reproduces the pinned book; cells are gradable.

## 2. Contamination check (reversal cohorts)

Baseline = X_trig_a `default` reversal book (70277 rows).
- PASS: X_trig_a/raw_reclaim -- 70277 rows, 70277/70277 identical
- PASS: X_trig_a/raw_up -- 70277 rows, 70277/70277 identical
- PASS: X_trig_b/raw_reclaim_hl -- 70277 rows, 70277/70277 identical

Verdict: PASS -- the trigger knob provably does not touch the reversal book.

## 3. Main table -- continuation cohort per trigger kind (slip 0.05)

`n_signals` sits next to every expectancy: a looser trigger fires more often, and more signals is not the same as more opportunity.

| trigger | n_signals | filled | missed | fill% | n_closed | clusters | exp R | lb95 (clust) | half-width | flags |
|---|---|---|---|---|---|---|---|---|---|---|
| default (HA, shipped) | 16813 | 16273 | 426 | 97.4% | 16221 | 511 | -0.161 | -0.178 | 0.017 | decisional |
| raw_up | 26746 | 25396 | 704 | 97.3% | 25327 | 511 | -0.178 | -0.196 | 0.018 | decisional |
| raw_reclaim | 12347 | 11978 | 320 | 97.4% | 11935 | 511 | -0.156 | -0.175 | 0.019 | decisional |
| raw_reclaim_hl | 10776 | 10454 | 281 | 97.4% | 10414 | 511 | -0.148 | -0.167 | 0.019 | decisional |

## 4. Mechanism check (mandatory)

D1 baseline: the shipped entry pays a median **1.56 ATR** above the setup's own low against **1.81 ATR** of risk -- a give-back ratio of ~**86%**. A kind that does not materially shrink `risk` has NOT fired nearer the low, and its expectancy may not be read as evidence about entry timing.

| trigger | mean risk | vs anchor | reading |
|---|---|---|---|
| default | 9.057 | 1.000x | anchor |
| raw_up | 7.421 | 0.819x | fires nearer the low (hypothesis tested) |
| raw_reclaim | 9.492 | 1.048x | **NOT nearer the low -- hypothesis untested by this cell** |
| raw_reclaim_hl | 9.791 | 1.081x | **NOT nearer the low -- hypothesis untested by this cell** |

## 5. Exit-reason mix (closed-filled continuation trades)

Tells a change in WHAT HAPPENS from a change in the R denominator: a smaller risk mechanically lifts R-to-target, but only a real improvement moves the target/stop MIX.

| trigger | n_closed | target% | stop% | momentum_flip% | time_stop% | avg hold |
|---|---|---|---|---|---|---|
| default | 16221 | 14.3% | 28.8% | 47.9% | 9.0% | 4.4 |
| raw_up | 25327 | 17.5% | 38.5% | 37.1% | 6.9% | 3.9 |
| raw_reclaim | 11935 | 13.4% | 27.2% | 49.4% | 10.0% | 4.6 |
| raw_reclaim_hl | 10414 | 12.9% | 25.8% | 50.8% | 10.5% | 4.7 |

## 6. Pre-registered verdict (one-shot; no grid extension)

Decisional iff n_closed >= 20, clusters >= 8, half-width <= 0.10R. POSITIVE iff some NON-HA kind has clustered 95% lb > 0 net of 0.05 ATR.

- default: lb=-0.178R (exp -0.161R, fill 97.4%, n=16221, cl=511, hw=0.017R) -> does not clear
- raw_up: lb=-0.196R (exp -0.178R, fill 97.3%, n=25327, cl=511, hw=0.018R) -> does not clear
- raw_reclaim: lb=-0.175R (exp -0.156R, fill 97.4%, n=11935, cl=511, hw=0.019R) -> does not clear [mechanism NOT engaged]
- raw_reclaim_hl: lb=-0.167R (exp -0.148R, fill 97.4%, n=10414, cl=511, hw=0.019R) -> does not clear [mechanism NOT engaged]

**VERDICT: NULL** -- no non-HA trigger clears clustered 95% lb > 0 net of 0.05 ATR. Firing nearer the low was the single mechanism D1 identified and Q8 corroborated; with it exhausted, entry TIMING joins entry PRICE as a falsified explanation. The remaining D1 finding (>10% of 'shallow pauses' retrace their entire leg) is the last structural candidate.

## 7. Caveats

- Replay-screened evidence tier only; nothing here is forward-confirmed.
- Only slip-0.05 dumps exist; the 0.10 robustness re-walk is conditional on POSITIVE.
- The pullback walk-back and shaved-head requirement remain HA-based; only the TRIGGER bar's test changed. A fully raw setup definition is a different, larger experiment.
- An undercut-and-reclaim ('spring') trigger was deliberately excluded: the setup's swing_low excludes the trigger bar, so such a trigger would place the stop above its own bar's low. Testing it needs a stop-geometry change too.
- The seeded clustered bootstrap is ticker-insertion-order sensitive at the 3rd decimal; rows are canonically sorted before every bound.

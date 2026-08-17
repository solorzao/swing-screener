# Q8 cont_retest_entry -- negative ceiling_atr_mult (retest-limit entry)

Pinned corpus as-of 20260703; slippage 0.05 ATR baked into realized_r; house ticker-clustered bootstrap (seeded, canonical ticker order); continuation cohort only. Pre-registration: docs/plans/2026-08-16-retest-entry-preregistration.md

- loaded `C_retest_a`: 8 shards, 261124 rows, variants ['default', 'retest_000', 'retest_015']
- loaded `C_retest_b`: 8 shards, 256534 rows, variants ['retest_030', 'retest_050', 'retest_075']
- loaded `D_dump`: 8 shards, 87090 rows, variants ['default']

## 1. Sanity anchor (gate)

C_retest_a `default` continuation book: n_signals=16813, filled=16273, missed=426, fill%=97.4%, n_closed=16221, clusters=511, expectancy=-0.1613R, clustered lb95=-0.1782R
- PASS: expectancy within +/-0.005 of -0.161R (-0.1613R)
- PASS: n_closed in [15900, 16500] (16221)
- PASS: clusters >= 500 (511)
- PASS: fill% in [95%, 99%] (97.4%)

Cross-walk identity vs D_dump default continuation: 16813/16813 keys matched (100.00%); only-in-sweep=0, only-in-D_dump=0
- per-column mismatches on matched keys: {'entry_price': 0, 'stop': 0, 'target': 0, 'risk': 0, 'realized_r': 0, 'hold_bars': 0, 'fill_status': 0, 'status': 0, 'exit_reason': 0, 'exit_date': 0}
- PASS: trade sets identical

**Anchor PASS** - the walk reproduces the pinned book; cells are gradable.

## 2. Contamination check (reversal cohorts)

Baseline = C_retest_a `default` reversal book (70277 rows).
- PASS: C_retest_a/retest_000 -- 70277 rows, 70277/70277 identical to baseline
- PASS: C_retest_a/retest_015 -- 70277 rows, 70277/70277 identical to baseline
- PASS: C_retest_b/retest_030 -- 70277 rows, 70277/70277 identical to baseline
- PASS: C_retest_b/retest_050 -- 70277 rows, 70277/70277 identical to baseline
- PASS: C_retest_b/retest_075 -- 70277 rows, 70277/70277 identical to baseline

Verdict: PASS -- the retest knob provably does not touch the reversal book.

## 3. Main table -- continuation cohort per ceiling_atr_mult (slip 0.05)

Negative mult = the limit sits BELOW the trigger close (a retest bid). fill% sits next to every expectancy: a cell that 'wins' on a sliver of fills must be visible as such.

| mult | kind | n_signals | filled | missed | inval | fill% | n_closed | clusters | exp R | lb95 (clust) | half-width | flags |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| +0.35 | chase (default) | 16813 | 16273 | 426 | 114 | 97.4% | 16221 | 511 | -0.161 | -0.178 | 0.017 | decisional |
| +0.00 | retest | 16784 | 14279 | 2395 | 110 | 85.6% | 14241 | 511 | -0.060 | -0.078 | 0.019 | decisional |
| -0.15 | retest | 16696 | 12194 | 4403 | 99 | 73.5% | 12163 | 511 | -0.035 | -0.055 | 0.020 | decisional |
| -0.30 | retest | 16485 | 9729 | 6668 | 88 | 59.3% | 9710 | 511 | -0.030 | -0.052 | 0.022 | decisional |
| -0.50 | retest | 15694 | 6376 | 9248 | 70 | 40.8% | 6368 | 511 | -0.015 | -0.044 | 0.029 | decisional |
| -0.75 | retest | 13524 | 3228 | 10248 | 48 | 24.0% | 3227 | 508 | -0.022 | -0.065 | 0.043 | decisional |

## 4. Selection artifact watch

As the limit drops, two things shrink the book for reasons that are NOT 'better entry price':

- **Degenerate zones** -- when the ceiling falls to/below the zone floor there is no tradable zone at all and the setup is never booked. Visible as n_signals falling vs the default.
- **Retrace selection** -- among booked setups, only those that pulled back fill. The survivors are a biased subset, not a random sample.

| mult | n_signals | vs default | signals lost (degenerate) | fill% | filled n | filled vs default |
|---|---|---|---|---|---|---|
| +0.35 | 16813 | +0 | 0 | 97.4% | 16273 | +0 |
| +0.00 | 16784 | -29 | 29 | 85.6% | 14279 | -1994 |
| -0.15 | 16696 | -117 | 117 | 73.5% | 12194 | -4079 |
| -0.30 | 16485 | -328 | 328 | 59.3% | 9729 | -6544 |
| -0.50 | 15694 | -1119 | 1119 | 40.8% | 6376 | -9897 |
| -0.75 | 13524 | -3289 | 3289 | 24.0% | 3228 | -13045 |

Reading rule (pre-registered): a cell whose fill% collapses is a SELECTION result, not an entry-price result, however good its expectancy looks.

## 5. Exit-reason mix per mult (closed-filled continuation trades)

| mult | n_closed | target% | stop% | momentum_flip% | time_stop% | avg hold (bars) | avg risk |
|---|---|---|---|---|---|---|---|
| +0.35 | 16221 | 14.3% | 28.8% | 47.9% | 9.0% | 4.4 | 9.048 |
| +0.00 | 14241 | 22.5% | 29.9% | 42.3% | 5.4% | 3.8 | 7.857 |
| -0.15 | 12163 | 24.8% | 31.6% | 39.4% | 4.2% | 3.5 | 7.140 |
| -0.30 | 9710 | 26.0% | 34.2% | 36.7% | 3.0% | 3.1 | 6.375 |
| -0.50 | 6368 | 27.0% | 37.9% | 33.0% | 2.1% | 2.7 | 5.564 |
| -0.75 | 3227 | 27.0% | 42.7% | 29.3% | 1.0% | 2.2 | 4.876 |

`avg risk` is the mechanism check: a lower ceiling shrinks `risk = ceiling - stop`, so the same absolute move books as a larger R.

## 6. Pre-registered verdict (one-shot; no grid extension)

Decisional iff n_closed >= 20, clusters >= 8, half-width <= 0.10R. POSITIVE iff some RETEST cell (mult <= 0) has clustered 95% lb > 0 net of 0.05 ATR.

- mult +0.35: lb=-0.178R (exp -0.161R, fill 97.4%, n=16221, cl=511, hw=0.017R) -> does not clear
- mult +0.00: lb=-0.078R (exp -0.060R, fill 85.6%, n=14241, cl=511, hw=0.019R) -> does not clear
- mult -0.15: lb=-0.055R (exp -0.035R, fill 73.5%, n=12163, cl=511, hw=0.020R) -> does not clear
- mult -0.30: lb=-0.052R (exp -0.030R, fill 59.3%, n=9710, cl=511, hw=0.022R) -> does not clear
- mult -0.50: lb=-0.044R (exp -0.015R, fill 40.8%, n=6368, cl=511, hw=0.029R) -> does not clear
- mult -0.75: lb=-0.065R (exp -0.022R, fill 24.0%, n=3227, cl=508, hw=0.043R) -> does not clear

**VERDICT: NULL** -- no retest cell clears clustered 95% lb > 0 net of 0.05 ATR. The retest-limit entry joins selection, timing, ordering, gates and entry-price-above-market as a falsified continuation lever; the parking rule stands and the grid is closed.

## 7. Caveats

- Replay-screened evidence tier only (pinned corpus, deterministic walk); nothing here is forward-confirmed.
- Only slip-0.05 dumps exist; the 0.10 robustness re-walk is conditional on POSITIVE.
- A retest limit is modelled as filling at `min(bar_high, ceiling)` on a bar that trades into the zone. Real retest fills also face queue position and adverse selection that OHLC replay cannot see, so a positive result here should be read as an upper bound on the mechanic.
- The seeded clustered bootstrap is ticker-insertion-order sensitive at the 3rd decimal; rows are canonically sorted before every bound.

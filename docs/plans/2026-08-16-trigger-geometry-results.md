# D1 — continuation trigger-geometry specification audit

Pinned corpus as-of 20260703; shipped `StrategyConfig` (freshness gate ON, so this describes the book actually traded). Geometry comes from the REAL `detect_last_bar`, never a re-implementation. Pre-registration: docs/plans/2026-08-16-trigger-geometry-diagnostic.md

- tickers walked: 511
- triggers recorded: 16868

## A — where we buy, relative to the setup's own low

The HA-lag question. `bars_since_low` counts bars from the pullback's lowest low to the trigger bar; the ATR columns say how far above that low the trigger close (and the price actually paid) sits.

| metric | n | mean | p10 | p25 | p50 | p75 | p90 |
|---|---|---|---|---|---|---|---|
| bars_since_low | 16868 | 1.89 | 1.00 | 1.00 | 2.00 | 2.00 | 3.00 |
| pullback_bars | 16868 | 3.36 | 2.00 | 3.00 | 4.00 | 4.00 | 4.00 |
| entry_above_low_atr | 16868 | 1.25 | 0.69 | 0.93 | 1.21 | 1.53 | 1.86 |
| ceiling_above_low_atr | 16868 | 1.60 | 1.04 | 1.28 | 1.56 | 1.88 | 2.21 |
| stop_distance_atr | 16868 | 1.85 | 1.29 | 1.53 | 1.81 | 2.13 | 2.46 |

## B — leg maturity at trigger

Is the trigger firing early in a leg or late in an exhausted one? `leg_bars`/`leg_extension_atr` measure from the ema_fast/ema_slow cross that started the leg; left-censored legs are excluded.

| metric | n | mean | p10 | p25 | p50 | p75 | p90 |
|---|---|---|---|---|---|---|---|
| leg_bars | 16868 | 58.22 | 11.00 | 21.00 | 42.00 | 80.00 | 127.00 |
| leg_extension_atr | 16868 | 6.20 | 0.79 | 2.39 | 5.16 | 8.83 | 13.05 |
| extension_atr | 16868 | 0.93 | 0.19 | 0.54 | 0.94 | 1.35 | 1.67 |
| retrace_frac | 16713 | 0.99 | 0.17 | 0.25 | 0.40 | 0.68 | 1.16 |

Left-censored legs excluded from B: 0 (0.0% of triggers).

## C — outcome by geometry (DESCRIPTIVE ONLY)

Joined 16221 of 16221 filled+closed book rows (100.0% match rate) on (ticker, entry_date).

> **This section cannot certify anything.** The 2026-07-03 rank sweep already falsified geometry-as-selector across nine orderings including freshness and pullback depth. Multiple comparisons are uncontrolled here by design; apparent winners are noise until separately pre-registered.

**bars_since_low** (quintiles, filled+closed default book)

| bucket | n | tickers | mean R |
|---|---|---|---|
| (0.999, 2.0] | 12496 | 511 | -0.167 |
| (2.0, 3.0] | 2588 | 504 | -0.140 |
| (3.0, 4.0] | 1137 | 446 | -0.147 |

**entry_above_low_atr** (quintiles, filled+closed default book)

| bucket | n | tickers | mean R |
|---|---|---|---|
| (-0.196, 0.86] | 3245 | 507 | -0.209 |
| (0.86, 1.103] | 3244 | 509 | -0.182 |
| (1.103, 1.326] | 3244 | 504 | -0.178 |
| (1.326, 1.62] | 3244 | 507 | -0.161 |
| (1.62, 3.651] | 3244 | 505 | -0.077 |

**leg_extension_atr** (quintiles, filled+closed default book)

| bucket | n | tickers | mean R |
|---|---|---|---|
| (-2.848, 1.866] | 3245 | 495 | -0.146 |
| (1.866, 3.961] | 3244 | 503 | -0.133 |
| (3.961, 6.373] | 3244 | 492 | -0.180 |
| (6.373, 9.956] | 3244 | 481 | -0.152 |
| (9.956, 130.217] | 3244 | 388 | -0.194 |

**extension_atr** (quintiles, filled+closed default book)

| bucket | n | tickers | mean R |
|---|---|---|---|
| (-1.2449999999999999, 0.44] | 3245 | 501 | -0.144 |
| (0.44, 0.792] | 3244 | 511 | -0.172 |
| (0.792, 1.096] | 3244 | 510 | -0.188 |
| (1.096, 1.441] | 3244 | 503 | -0.161 |
| (1.441, 1.999] | 3244 | 508 | -0.141 |

**retrace_frac** (quintiles, filled+closed default book)

| bucket | n | tickers | mean R |
|---|---|---|---|
| (0.012400000000000001, 0.228] | 3215 | 427 | -0.210 |
| (0.228, 0.337] | 3217 | 481 | -0.179 |
| (0.337, 0.488] | 3212 | 491 | -0.147 |
| (0.488, 0.772] | 3215 | 498 | -0.154 |
| (0.772, 3581.506] | 3215 | 493 | -0.120 |

---

## Findings (hand-written; regenerating the data above overwrites this file)

### 1. 86% of the risk budget is spent before we enter

The decisive numbers, at the median of 16,868 triggers over 511 tickers:

| quantity | median ATR |
| --- | --- |
| price actually paid, above the setup's low (`ceiling_above_low_atr`) | 1.56 |
| total risk taken (`stop_distance_atr`) | 1.81 |

The stop sits a fixed `stop_buffer_atr = 0.25` below the swing low, so **1.56 of every
1.81 ATR risked — about 86% — is the distance price had already travelled off the low
before the trigger fired.** Only the remaining ~14% is structural buffer.

This is not a statistical artifact; it is a design property of anchoring the stop at the
swing low while triggering on a *smoothed* series. Heiken-Ashi cannot flip at the low —
it flips a median of **2 bars** later, by which point the median setup has already
retraced 1.21 ATR of the bounce. Every trade therefore opens by risking the move it just
missed.

It also explains Q8 exactly. Bidding 0.50 ATR below the flip close recovered +0.146R
because it was directly reclaiming this give-back — and it recovered ~91% of the gap to
breakeven while still never clearing zero.

### 2. The "shallow pause" is not reliably shallow

`retrace_frac` — how much of the interrupted leg the pause gives back — has a median of
0.40 but a **p90 of 1.16**. More than a tenth of the setups the detector calls a shallow
pullback have retraced **the entire leg or more**. Those are not continuations; they are
broken structures that still satisfy the `ema_fast > ema_slow and close > ema_slow`
precondition, because two EMAs cannot tell a pause from a reversal.

### 3. The freshness gate is barely binding

`extension_atr` runs at a median of 0.94 and a p90 of 1.67 against a `max_extension_atr`
gate of **2.0**. The anti-chase gate clips only the extreme tail — it is not the thing
standing between the strategy and a late entry, despite being the knob the optimizer
sweeps. Meanwhile `leg_extension_atr` (median 5.16, p90 13.05) says triggers routinely
fire with price already 5+ ATR above the leg's origin.

### 4. Section C changes nothing, exactly as pre-registered

Every quintile of every geometry metric is negative. Nothing here rescues the book, which
is what the 2026-07-03 rank sweep already established across nine orderings. The mild
gradients visible above are uncontrolled and multiply-compared; they are **not** evidence
and must not be traded on. In particular the apparent improvement along
`entry_above_low_atr` runs opposite to Finding 1 and should be treated as noise or
confounding until someone pre-registers a test of it — not as "enter later".

## What this implies

The edge file now requires that any continuation proposal be a different **setup or
exit**, entry price having been falsified in both directions. This audit says where a
setup redesign would have to bite:

1. **Trigger nearer the low.** The HA flip's 2-bar lag costs ~1.2 ATR of give-back before
   entry. A trigger defined on the raw reversal bar (or on a level reclaim) would attack
   the 86% number directly. Q8 is the evidence that reclaiming even part of it matters.
2. **A pause/reversal discriminator.** Something beyond two EMAs must separate a genuine
   shallow pause from a full leg retrace, or >10% of the book will keep being
   counter-trend entries wearing a continuation label.

Both are genuine setup changes and each would need its own pre-registered replay clearing
clustered 95% lb > 0 net 0.05 ATR. Neither is authorised by this document, and neither
un-parks continuation.

**The honest null hypothesis remains live:** the two defects above may be *why* the thesis
loses, or they may simply be what an HA pullback strategy looks like on this universe
while the real problem is that shallow-pullback continuation carries no edge here at all.
This audit cannot distinguish those, and nothing in it should be read as a prediction that
a redesign would work.

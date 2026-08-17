# D1 — trigger-geometry specification audit (continuation)

**Date:** 2026-08-16
**Status:** pre-registered. DESCRIPTIVE / hypothesis-generating — this experiment cannot
certify an edge, by construction (see Discipline).

## The question

`edge/continuation.md`'s deepest open question:

> **Why does the bull slice — the thesis's home regime — stay negative at scale?** With
> n=2484 and a lower bound of −0.20R… Before iterating on filters, confirm the signal
> definition (HA flip out of a shallow pause) is firing where we think it is, and not
> systematically entering after the trend leg has already exhausted.

Every lever on the *entry price* axis is now falsified in both directions (Q6 above
market, Q8 below it). The edge file's standing note is that a future proposal must be a
different **setup or exit**. This audit asks whether the setup is specified the way we
think it is — it is not a search for a profitable slice.

## The lead Q8 handed us

Q8 found that bidding 0.50 ATR *below* the flip-bar close recovers **+0.146R** (−0.161 →
−0.015). That is direct evidence the shipped entry is systematically **too high relative
to the setup's own low** — and the mechanism confirmed it (average `risk` 9.05 → 4.88,
target exits 14.3% → 27.0%).

Heiken-Ashi is a smoothed series. A bullish HA flip cannot, in general, coincide with the
true pullback low; it must lag it. **That lag has never been measured.** If the flip
typically fires several bars and a large fraction of an ATR above the actual low, then the
defect is the *trigger's timing within the setup*, not the pullback concept — and a
non-HA trigger is a designable fix. If the lag is small, the thesis is structurally dead
and continuation should stay parked permanently.

## Measurements (all descriptive, per continuation trigger, pinned corpus)

**A — HA lag / where we buy relative to the setup's low**

- `bars_since_low` — bars from the pullback's swing-low bar to the trigger bar.
- `entry_above_low_atr` — `(trigger_close − swing_low) / atr`.
- `ceiling_above_low_atr` — `(entry_ceiling − swing_low) / atr` (the price actually paid).
- `stop_distance_atr` — `(entry_ceiling − stop) / atr`, i.e. the R denominator.

**B — leg maturity at trigger**

- `leg_bars` — bars since `ema_fast` last crossed above `ema_slow` (the leg's origin).
- `leg_extension_atr` — `(trigger_close − close[leg_origin]) / atr`.
- `extension_atr` — `(trigger_close − ema_fast) / atr` (the shipped freshness metric).
- `retrace_frac` — how deep the pause was against the leg it interrupts:
  `(leg_high − swing_low) / (leg_high − close[leg_origin])`.

**C — outcome conditioning** — realized R by decile of each of the above, on the
filled+closed default book, reported with ticker-clustered bounds and cell counts.

## Discipline — why this cannot certify anything

The rank sweep (2026-07-03, n=16,219, 511 clusters) already tested nine candidate
orderings **including freshness and pullback depth/length** and found no ordering with a
positive-expectancy top quintile. Slicing this book by geometry to find a winner is a
**settled null**. Section C is therefore reported as description only:

- No cell in C may be promoted, shipped, or used to justify un-parking.
- Any slice that looks promising requires a SEPARATE pre-registered confirmation with a
  corrected bound, exactly as Q3/Q6/Q8 did.
- Multiple comparisons are uncontrolled here by design; deciles across eight metrics will
  produce apparent winners by chance and must be read as noise until confirmed.

The decision this audit informs is binary and structural:

> Is there a *designable different setup* (e.g. a non-HA trigger that fires nearer the
> true low), or is the HA pullback-continuation thesis structurally dead on this universe?

Sections A and B answer that. Section C only colours it.

## Scope and known limits

- Continuation cohort, default variant, pinned corpus as-of `20260703`, slip 0.05.
- Geometry is recomputed post-hoc from the cached frames and joined to the existing
  `D_dump` book on `(ticker, entry_date)`, where `entry_date` for a filled row is the fill
  bar — i.e. the bar AFTER the trigger. A **missed** booking re-anchors `entry_date`, so
  missed rows will not join; the outcome section is therefore scoped to the
  **filled** book and the join match-rate is reported as a gate on that reading.
- Leg origin is defined by the EMA cross, which is itself a lagging construct. A leg whose
  origin predates the cached history is left-censored and excluded from B (count reported).

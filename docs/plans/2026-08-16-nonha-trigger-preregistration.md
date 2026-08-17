# Q9 `cont_nonha_trigger` — pre-registration

**Date:** 2026-08-16
**Status:** pre-registered BEFORE implementation and before any full walk. Binding.

## The hypothesis, and why it is not another knob

D1 measured that the shipped entry pays a median **1.56 ATR above the setup's own low**
against a median total risk of **1.81 ATR** — ~86% of every 1R risked is give-back of a
move that already happened. The cause is structural: Heiken-Ashi is smoothed, so a bullish
HA flip **cannot** fire at the low; it fires a median of 2 bars later.

Q8 confirmed the give-back matters. Bidding 0.50 ATR below the flip close recovered
**+0.146R** (−0.161 → −0.015) — but it bought that improvement by only filling on setups
that retraced, collapsing fill from 97.4% to 40.8%, and still never cleared zero.

**The hypothesis:** a trigger that fires *nearer the low in the first place* should capture
Q8's price improvement **without** Q8's fill collapse — because it enters on the bar it
fires on, rather than waiting for price to come back.

This is a **setup** change (the trigger's definition), which is what `edge/continuation.md`
now requires; entry price is falsified in both directions and is not what is varied here.

## The three trigger kinds (exact definitions)

All are raw-candle conditions replacing the HA-flip test at the trigger bar. Everything
else is byte-identical to the shipped detector: the same uptrend precondition, the same
HA-based pullback walk-back and shaved-head requirement, the same shallow-pullback gate,
the same zone/stop/target geometry, the same quality gates.

Let `last` be the trigger bar and `prev` the bar before it.

| `trigger_kind` | condition | intent |
| --- | --- | --- |
| `ha_flip` (default) | `last["bullish"]` (HA) | shipped anchor |
| `raw_up` | `close > open` | earliest possible — the first raw up bar off the pause |
| `raw_reclaim` | `close > open` AND `close > prev["high"]` | a decisive reclaim of the prior bar's range |
| `raw_reclaim_hl` | `raw_reclaim` AND `low > prev["low"]` | reclaim **plus** a higher low (a structural turn, not a spike) |

Deliberately excluded: an undercut-and-reclaim ("spring") trigger. The setup's
`swing_low` is computed over the pullback bars **excluding** the trigger bar, so a trigger
that undercuts that low would place the stop above its own bar's low — already breached
intrabar. Testing it correctly needs a stop-geometry change too, which would confound the
trigger question with a risk-geometry question. Out of scope; noted for a future design.

## Grid (one-shot; closed)

Two walks, pinned corpus as-of `20260703`, slippage 0.05 ATR:

- `X_trig_a`: `default` (anchor) · `raw_up` · `raw_reclaim`
- `X_trig_b`: `raw_reclaim_hl`

No extension, no re-cut on a near miss, no post-hoc trigger variants.

## Decision rule (binding)

Continuation cohort only, 0.05 ATR slippage, house ticker-clustered bootstrap over
canonically sorted rows.

- **Decisional** iff `n_closed >= 20` AND `n_clusters >= 8` AND half-width
  (`expectancy − clustered lb`) `<= 0.10R`.
- **POSITIVE** iff at least one non-HA trigger is decisional AND its clustered 95% lower
  bound `> 0`.
- **NULL** otherwise.

POSITIVE caps at **replay-screened**. It does not un-park continuation: per the Q6/Q8
precedent, promotion additionally requires a 0.10-slippage robustness re-walk and a
forward variant slot, and un-parking stays a separate human-gated decision on forward
evidence.

## Mandatory mechanism check (a gate on interpretation, not on grading)

An experiment that does not do what it claims cannot be read as testing the hypothesis.
Per trigger kind, report the D1 geometry on the signals it actually produces:

- `ceiling_above_low_atr` — the price paid above the setup's low (D1 median: **1.56**)
- `stop_distance_atr` — total risk (D1 median: **1.81**)
- the give-back ratio `ceiling_above_low_atr / stop_distance_atr` (D1 median: **~86%**)

**A trigger kind that does not materially reduce the give-back ratio has not tested this
hypothesis**, whatever its expectancy does — and its expectancy must not be read as
evidence about entry timing. This is stated in advance precisely so a lucky cell cannot be
retold as a mechanism story afterwards.

## Gates that must pass before any cell is graded

1. **Sanity anchor** — the `X_trig_a` `default` continuation book must reproduce the pinned
   book (−0.161R ± 0.005, `n_closed` ∈ [15 900, 16 500], clusters ≥ 500, fill ∈ [95%, 99%])
   and be row-identical to `D_dump` on `(ticker, entry_date, opened_date)`. Fail → STOP.
2. **Contamination** — `trigger_kind` is continuation-only, so the reversal book must be
   row-identical across all cells.

## Failure modes named in advance

- **Signal flood.** `raw_up` is a much looser condition than an HA flip and will fire far
  more often. More signals is not better; `n_signals` is reported beside every expectancy,
  and a large increase should be read as admitting noise, not as finding opportunity.
- **The dilution trap.** A trigger firing nearer the low shrinks `risk = ceiling − stop`.
  Stopped trades are −1R by construction regardless, but the *structural* target is a fixed
  price, so a smaller risk mechanically increases R-to-target. Some expectancy improvement
  is therefore arithmetic, not skill. The exit-reason mix (target% vs stop%) is reported so
  a change in *what actually happens* can be told apart from a change in the denominator.
- **Earlier means wronger.** Firing before HA confirms should produce more false starts.
  Expect stop% to rise; the question is whether the improved R-per-target-hit outweighs it.

## RESULTS — graded 2026-08-16: **NULL**

Both walks completed (16/16 shards, zero failures) and were graded against the rule above,
unchanged. Full table: `docs/plans/2026-08-16-nonha-trigger-results.md`.

No non-HA trigger clears clustered 95% lb > 0. The mechanism check is what makes the
result interpretable, and it split the grid in two:

- **`raw_up` engaged the mechanism and lost.** Mean risk 0.819× the anchor — it genuinely
  fired nearer the low — and expectancy got *worse* (−0.178R, lb −0.196R vs the anchor's
  −0.161R). It also flooded, 26,746 signals vs 16,813 (+59%), with stop exits jumping
  28.8% → 38.5%. Both failure modes named in the pre-registration ("signal flood",
  "earlier means wronger") occurred exactly as described.
- **`raw_reclaim` / `raw_reclaim_hl` never engaged it.** Mean risk 1.048× and 1.081× the
  anchor — *higher*, because demanding a close above the prior bar's high puts the trigger
  bar FURTHER above the low, not nearer. Their milder expectancies (−0.156R, −0.148R) are
  therefore **not** evidence about entry timing; they are a selection effect from firing
  less often (10,776 signals vs 16,813). Per the pre-registration this reading was fixed in
  advance, and both cells still fail the bar regardless.

## What a NULL means (as written before the walk)

If firing nearer the low — the single mechanism D1 identified and Q8 corroborated — also
fails to clear the bar, then entry timing is exhausted as an explanation. The remaining
D1 finding (>10% of "shallow pauses" retrace their entire leg) would become the last
structural candidate, and if that also fails, the honest conclusion is that HA
pullback-continuation carries no edge on this universe and the parking should be
considered permanent rather than provisional.

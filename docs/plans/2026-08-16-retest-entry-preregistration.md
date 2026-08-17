# Q8 `cont_retest_entry` — pre-registration

**Date:** 2026-08-16
**Status:** pre-registered BEFORE the full walk. Decision rule below is binding.

## The question

Continuation surfacing is parked. The parking rule (`config.StrategyConfig.surface_continuation`,
`edge/continuation.md`) names exactly one re-enable path:

> RE-ENABLE only for a fundamentally different continuation entry mechanic clearing the
> replay bar: clustered 95% lb > 0 net 0.05 ATR.

Q6 swept `ceiling_atr_mult` over 0.15–0.65 and graded NULL (best cell −0.103R, clustered
lb −0.120). But **every Q6 cell is a limit ABOVE the trigger close** — `ceiling =
trigger_close + mult × ATR` with `mult > 0`. The whole sweep varied *how hard we chase the
flip bar*. It settled the chase family; it did not test a different mechanic.

**A negative `ceiling_atr_mult` inverts the mechanic.** The limit sits BELOW the trigger
close, so the next bar must retrace into it to fill. That changes two things at once:

1. **Price** — a lower entry with the stop unchanged (`swing_low − stop_buffer × ATR`)
   shrinks `risk = ceiling − stop`, so the same absolute move resolves as a larger R.
2. **Selection** — only setups that actually pull back get taken. Setups that run away
   register as `missed` and never enter the book.

That is a genuinely different entry mechanic, not another point on the chase axis.

## Grid (one-shot; closed)

Two walks of three variants, pinned corpus as-of `20260703`, slippage 0.05 ATR.

| walk | variant | `ceiling_atr_mult` | meaning |
| --- | --- | --- | --- |
| `C_retest_a` | `default` | +0.35 | shipped default — **sanity anchor** |
| `C_retest_a` | `retest_000` | 0.00 | limit exactly at the trigger close |
| `C_retest_a` | `retest_015` | −0.15 | shallow retest |
| `C_retest_b` | `retest_030` | −0.30 | medium retest |
| `C_retest_b` | `retest_050` | −0.50 | deep retest |
| `C_retest_b` | `retest_075` | −0.75 | very deep retest |

The grid is closed. No extension, no re-cut on a near miss.

## Decision rule (binding)

Graded on the **continuation** cohort only, at 0.05 ATR slippage, house ticker-clustered
bootstrap over canonically sorted rows.

- **Decisional** iff `n_closed >= 20` AND `n_clusters >= 8` AND half-width
  (`expectancy − clustered lb`) `<= 0.10R`. Cells failing this are reported but cannot
  carry the verdict.
- **POSITIVE** iff at least one retest cell (`mult <= 0`) is decisional AND its clustered
  95% lower bound `> 0`.
- **NULL** otherwise.

**POSITIVE does not re-enable surfacing.** It caps at replay-screened, and per the Q6
precedent promotion additionally requires a 0.10-slippage robustness re-walk and a
forward variant slot. Un-parking stays a separate human-gated decision on forward
evidence.

## Gates that must pass before any cell is graded

1. **Sanity anchor.** The `C_retest_a` `default` continuation book must reproduce the
   pinned book: expectancy −0.161R ± 0.005, `n_closed` ∈ [15 900, 16 500], clusters ≥ 500,
   fill% ∈ [95%, 99%] — and be row-identical to the existing `D_dump` default continuation
   book on `(ticker, entry_date, opened_date)`. Fail → STOP, grade nothing.
2. **Contamination.** `ceiling_atr_mult` is continuation-only, so the reversal book must be
   row-identical across all six cells.

## The artifact this experiment is most likely to produce

**A tight retest cell that "wins" on a sliver of fills.** As the limit drops, fill% falls
and the surviving trades are increasingly a selected subset — the ones that pulled back,
which is not a random sample of setups. So:

- `fill%` and `n_signals` are reported NEXT TO every expectancy.
- A cell whose fill% collapses is to be read as a *selection* result, not an entry-price
  result, no matter how good its expectancy looks.
- The zone can also go degenerate (`floor >= ceiling` → no trade at all) at deep negative
  values, which shows up as `n_signals` falling. That is a real cost: those setups aren't
  traded worse, they're not traded at all.

A cell that clears lb > 0 on a small, heavily-selected slice is **not** an edge; it is the
same "expectancy wins while never filling" failure Q6 was built to catch.

## Disclosure

A 25-ticker feasibility probe was run before this pre-registration, to confirm negative
ceilings produce a non-degenerate book and to time the walk. It showed default −0.14R /
`retest_000` −0.05R / `retest_015` −0.03R with fill 97%/84%/72%, all lower bounds still
below zero. It informed **runtime and grid feasibility only**; the decision rule above is
set independently of it and is not tuned to those numbers. The probe is ~5% of the corpus
and is superseded by the full walk.

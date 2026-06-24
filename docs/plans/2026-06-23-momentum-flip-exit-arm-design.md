# Momentum-flip exit arm — design

**Date:** 2026-06-23
**Status:** approved (brainstorm), ready to implement

## Why

A full edge diagnosis of the continuation and reversal books (offline `replay_book`
over broad S&P cached baskets, post the #58–#63 honesty fixes) found:

- **Continuation has no edge as specified** (~−0.13 to −0.17R, ~30–33% win): 76% of
  trades lose, ~49% via `momentum_flip` exiting at −0.30R right after entry. The leak
  is the *entry*, not the exits/targets.
- **Reversal is the closest to an edge** but its sign **flips between baskets**
  (+0.027R on one, −0.082R on another) — i.e. it is **within sampling noise**, not a
  robust edge.
- **Exit A/B** (monkeypatched `evaluate_exit` over the trusted harness): reordering
  target-before-flip changes nothing (flips don't coincide with target hits — a
  hypothesis we *refuted*); `flip-if-losing` and `flip-gated` don't beat plain
  removal; **`no-flip` is the best rework**, helping reversal most (−0.082→−0.040R,
  win 33%→41%) and never clearly worse.

**Conclusion:** the `momentum_flip` exit is a *mild, consistent drag* — but every
effect we can chase offline is smaller than the confidence interval. Offline replay
on ~40–60-name / ~2y baskets cannot confirm a sub-0.1R edge. So we should **not
hard-switch the exit on noisy offline data.** Instead, instrument the change as a
live experiment arm and let real, accumulating forward evidence decide (North Star
#1 *evidence gates*, #7 *measure what you'd trade*).

## What we are building

A `no_flip` **exit arm** in the parallel-arm shadow book: same entries/fills/dates as
`baseline`, exit management identical **except** the `momentum_flip` check is disabled.
`breakdown(trades, "arm")` (dashboard + replay) then A/Bs `baseline` vs `no_flip` over
the live book as it accumulates — same-sample, regime-matched, no offline guess.

This is deliberately *not* a config default change and *not* an entry change. It adds
one measurement; it ships zero behavior change to what's surfaced or executed.

## Design

1. **`StrategyConfig.momentum_flip_exit: bool = True`** (new field, default preserves
   today's behavior). Lives next to the other exit knobs.
2. **`signals/exits.evaluate_exit`**: gate the `shaved_head` branch on
   `cfg.momentum_flip_exit`. When `False`, fall through to target/time. Stop and target
   ordering, prices, and tiers are untouched.
3. **`pipeline/arms.build_arms`**: add
   `"no_flip": replace(base, partial_frac=0.0, momentum_flip_exit=False)`. Pinning
   `partial_frac=0.0` mirrors `baseline`, so the two differ **only** in the flip — a
   clean A/B. No other arm changes.

Nothing else in the pipeline changes: `advance_open` already walks each open trade
under its own arm config, and the shadow book already books one fill per arm.

## Testing (TDD)

- `evaluate_exit` returns `momentum_flip` on a `shaved_head` bar when
  `momentum_flip_exit=True`, and **HOLDs** (or proceeds to target/time) when `False`.
- `evaluate_exit` still hard-stops and targets regardless of the flag.
- `build_arms` includes a `no_flip` arm with `momentum_flip_exit=False` and
  `partial_frac=0.0`, and `baseline` keeps `momentum_flip_exit=True`.
- Full suite + ruff + mypy green; the existing arms/exits/shadow tests unchanged.

## Honest caveat

This does **not** claim `no_flip` is better — offline evidence is too thin to say.
It makes the question answerable on real data. The strategy-direction decision
(lean reversal vs. re-spec / retire continuation) stays open and now waits on the
live shadow book (dashboard *Screener Performance*), this arm's A/B, and the
qualitative analyst loop — not more offline knob-twiddling. See the memory note
`project-continuation-edge-diagnosis`.

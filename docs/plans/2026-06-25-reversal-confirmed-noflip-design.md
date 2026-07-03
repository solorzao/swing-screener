# Reversal: surface CONFIRMED-only + no-flip exit (design)

2026-06-25

## Motivation

A replay study (branch `feat/outside-bar-trigger-experiment`, scripts/replay_reversal_*.py)
found the only cost-robust edge in the system: **CONFIRMED-strength reversals run with the
momentum-flip exit OFF**. Over 503 names:

| subset | gross | @0.05 ATR slip | @0.10 ATR slip |
|---|---|---|---|
| CONFIRMED reversal, no-flip | +0.161R (95%low +0.054) | +0.125R (95%low **+0.017**) | +0.089R (95%low -0.020) |
| EARLY reversal, no-flip | +0.032R (95%low -0.002) | -0.001R | -0.033R |
| continuation (any) | -0.12R | worse | worse |

CONFIRMED survives realistic slippage with significance intact; EARLY is breakeven-to-negative
and is ~92% of the reversal book (it dilutes the average). Continuation is dead at every level.

> **2026-07 postscript.** This table omitted the CONFIRMED cohort's sample size — a violation
> of the "every claim carries its n" principle flagged by the 2026-07-01 audit. The current
> re-runnable evaluation (`scripts/replay_fill_window.py`, 511 names, net of 0.05 ATR) carries
> it: under the original one-bar fill window, CONFIRMED = +0.124R (95%low +0.020), **n=746,
> 392 clusters, fill rate 14%**; under the realistic 5-bar resting-limit window shipped in
> PR #78, CONFIRMED = **+0.071R (95%low +0.013), n=2,231, 503 clusters, fill rate 42%** — the
> honest tradable number. The fragility noted here (95%low goes negative at 0.10 slip) stands.

## Changes (both behind config flags, reversible)

### 1. CONFIRMED-only reversal surfacing
- `StrategyConfig.reversal_surface_confirmed_only: bool = True`.
- `notify.select.reversal_picks` gains `confirmed_only: bool = False`; when set, adds
  `Signal.strength == "confirmed"` to the WHERE clause.
- The digest orchestrator (`notify.run`) passes `cfg.reversal_surface_confirmed_only`.
- EARLY reversals are still detected, scored, and shadow-booked — only hidden from the
  digest. The learning loop is preserved and the flag is reversible.

### 2. No-flip exit for the reversal book
- `StrategyConfig.reversal_momentum_flip_exit: bool = False` (continuation's
  `momentum_flip_exit=True` is unchanged).
- `OpenTrade` gains `play_type: str = "continuation"` (defaulted so existing constructors and
  the live `exitcheck` path — all continuation today — are unaffected).
- `evaluate_exit` selects the flip flag by `trade.play_type`: reversal trades use
  `reversal_momentum_flip_exit`, others use `momentum_flip_exit`.
- `shadow.advance_open` passes `play_type=pt.play_type` (PaperTrade carries it). This makes the
  reversal **baseline** arm no-flip (per the decision); the `no_flip` arm stays a clean A/B for
  continuation. For reversal, baseline == no_flip (the arm just corroborates the choice).

## Scope / non-goals
- Reversal is digest + shadow only today (the autonomy gate hasn't armed it for live trading),
  so these are surfacing + go-forward-policy changes, not immediate real-money changes.
- FOLLOW-UP (when reversal arms for live trading): the real `Trade` model has no `play_type`,
  so `exitcheck` currently defaults reversal real-trades to continuation's flip setting. Thread
  `play_type` onto `Trade` + `exitcheck` before reversal goes live, or live reversal exits will
  use the flip. Tracked here so it isn't lost.

## Verification
- TDD unit tests: `reversal_picks(confirmed_only=True)` drops EARLY; `evaluate_exit` suppresses
  momentum_flip for a reversal trade by default and still fires for continuation (and can be
  re-enabled via the flag).
- Full suite + ruff + mypy green.

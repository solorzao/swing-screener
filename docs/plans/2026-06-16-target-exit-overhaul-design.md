# Target / exit overhaul — design

**Date:** 2026-06-16
**Status:** Proposed
**Scope:** Phase 2/3 of the target/indicator overhaul ([[project-target-indicator-overhaul]]).
The indicator scorers (RSI, MACD) already shipped (#29); this is the target/exit half.

## Goal

Un-cap the right tail of the momentum strategy *without amputating it*: replace the blind
fixed-2R continuation target with a structure-aware target, then add a small conditional
partial scale-out and a trailing stop on the runner. Everything is judged by shadow-book
`expectancy_r` (analytics/performance.py), and every new level is anchored to the SAME
fill-price/stop the shadow book realizes R off (`risk = fill.price - stop`) — not the zone
midpoint. Structural levels are read off STANDARD candles (HA smears real highs/lows).

## Sequencing (note: the structure target ships FIRST, before the refactor)

The pieces have different prerequisites, so they're ordered by value-per-risk:

### Step A — Structure-aware continuation target (NO shadow-book refactor)
`signals/entry_zone.compute_zone` currently sets `target = reference + 2.0*risk` (blind 2R).
Replace with a fill-anchored cascade (a *single* terminal target — the shadow book already
handles one target, so this needs no refactor and is immediately measurable):
1. **Nearest prior swing high / resistance** above the fill, read off STANDARD `high` over a
   swept lookback (~20-40 bars). Pivot = a local high with N bars lower on each side.
2. **Fallback — ATR measured move** when no qualifying pivot is overhead: `fill + 2.0*ATR`.
3. **Minimum R:R floor:** require `(target - fill)/risk >= 1.5`; else push to the 1.5R level.
   Keep the existing degenerate-zone `None` guards.
`compute_zone` will need the recent highs — pass the relevant frame slice (or precomputed
swing-high column from frame.py) in. Reversal engine target unchanged (already structure-aware).
**Ships as its own PR; measurable on its own.**

### Step B — Shadow-book fractional-close refactor (the prerequisite for C/D)
Partials and path-dependent trails aren't expressible today: `exits.evaluate_exit` returns ONE
terminal `ExitDecision`; `shadow.advance_open` books one all-or-nothing `realized_r`. Add:
- **PaperTrade fields** (Alembic migration): `partial_done` (bool), `partial_price`/`partial_r`
  (the booked first leg), `remaining_frac` (float, 1.0 → e.g. 0.67 after the partial),
  `high_water` (highest high since fill — the Chandelier reference).
- **`advance_open`**: on each bar — book the partial when the first target is hit (set
  `remaining_frac`, `partial_r`, `partial_done`); ratchet `high_water` + the trailing stop up
  (never down); close the remainder on stop / momentum_flip / time_stop. `realized_r` becomes a
  **size-weighted blend**: `partial_frac*partial_r + remaining_frac*final_r`.
- **`exits.evaluate_exit`**: add a `target_partial` (non-terminal) outcome. Keep the LIVE
  `pipeline/exitcheck.py` path BACKWARD-COMPATIBLE (it emits real-trade exit alerts) — partials
  are a shadow-book/QC feature first; live scale-out alerts are a later phase.
- **Reconciliation test:** with the partial fraction = 0 (feature off), `realized_r` reproduces
  the old all-or-nothing value EXACTLY. Plus: trail is monotonic-up and never reads a future bar.

### Step C — Conditional partial + breakeven stop (needs B)
At the first target, scale out a small fraction and protect the rest:
- **Fraction:** default **33%** (config, swept). **Conditional:** only scale when HA momentum is
  already softening (shrinking body / no `shaved_bottom`) — keep more on while the trend is strong.
- After the partial, **move the stop to breakeven** (the fill) so the runner can't give the leg
  back. The runner then exits on momentum_flip / breakeven-stop / time_stop.

### Step D — Trailing-stop bake-off (needs B/C)
Add a Chandelier trail (`high_water - m*ATR`, flat `m=3.0/3.5`) as a config-selectable option for
the runner, and measure the arms: **(0) momentum_flip + breakeven only**, **(1) Chandelier**. Ship
at most one winner; if (0) wins, no Chandelier. (Vol-widening `m` is a later experiment that must
beat flat-m head-to-head.)

### Step E — Reversal engine (needs B)
Mirror the conditional partial + the winning trail on the reversal runner; keep the 0.786-Fib target.

## Key decisions (flag if you'd change these)
- **Structure target lookback** starts as a sweep (20-40 bars), justified on continuation
  pullback geometry — NOT borrowed from the reversal engine's 31-bar constant.
- **Partial = 33%, conditional** on HA softening (not a flat 50% — that caps the runner).
- **Post-partial stop = breakeven.**
- **Partials are shadow-book-only initially**; the live exit-alert path (exitcheck.py) stays
  terminal until a later phase. (Decision: keep live alerts simple first vs. emit scale-out alerts now.)
- **Measurement:** each step A/B'd vs the prior shipped baseline on the same universe/date range,
  with a margin over the optimistic-fill bias + an out-of-time holdout (multiple-comparisons guard).

## Out of scope
Alligator / VWAP / divergence (dropped). Live-path partial alerts (deferred). Anchored-VWAP trail
(dropped with VWAP). Vol-regime-scaled Chandelier `m` (later, only if flat-m wins).

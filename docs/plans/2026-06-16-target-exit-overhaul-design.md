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

#### Step C implementation addendum (parallel-arm dual-book) — 2026-06-16

A live single-config shadow book can only compare a partial arm *temporally* (future
partial trades vs the historical all-or-nothing record) — confounded by regime, and short
of the design's "same universe/date range" bar. So Step C also lays the **measurement rail**:
a **parallel-arm dual-book**. Every fill opens one `PaperTrade` **per arm**, identical fill
economics, tagged with `arm`; each open trade is advanced under *its* arm's config. The
existing `breakdown(trades, "arm")` then gives apples-to-apples per-arm `expectancy_r` on the
exact same tickers and dates. (Forward-only — it accrues over weeks; no instant holdout yet.
A replay backtester for the out-of-time holdout is a later, optional build.)

- **`PaperTrade.arm`** (`String(32)`, default `"baseline"`, indexed) + reversible migration.
  Both filled AND missed/invalidated rows are duplicated per arm so each arm is a *complete*
  book (`fill_rate`/`n_total` correct per arm; storage cost is trivial).
- **Arm roster** (`pipeline/arms.py::build_arms(base)`): `baseline` (`partial_frac=0.0`) and
  `partial33_cond` (`partial_frac=0.33`, `partial_require_softening=True`). `baseline` is built
  with `replace(base, partial_frac=0.0)` so it stays all-or-nothing even if the base default
  later changes. Adding the Step D Chandelier arm = one more entry.
- **`open_from_signals(..., arms=("baseline",))`** opens one tagged trade per arm name.
  **`advance_open(session, latest_bars, arms, today)`** takes either a single `StrategyConfig`
  (normalized to `{"baseline": cfg}` — back-compat for existing callers/tests) or a
  `{arm: cfg}` mapping, and advances each trade under `arms[pt.arm]`.

**Conditional gate (`partial_require_softening`, default `False`):** at a target touch, scale
out only when HA momentum is **softening**:
`softening = (not shaved_bottom) or body_shrinking`, where `body_shrinking` = the HA
`body_frac` is below the prior bar's (computed in `run._bar_row`, which sees the full frame;
`shaved_bottom` is added to `_BAR_KEYS`). When **strong** (`shaved_bottom and not
body_shrinking`) at the target, **suppress the target exit and HOLD the full position** (let
the winner ride on its original stop) — re-evaluated each bar, so it partials the moment it
softens while still at/above target. (Edge: a strong target-touch that is *also* a time_stop
keeps riding — you don't time-stop a winner breaking to new ground; documented, rare.)
The unconditional path (`partial_require_softening=False`) is exactly the Step B behavior.

### Step D — Trailing-stop bake-off (needs B/C)
Add a Chandelier trail (`high_water - m*ATR`, flat `m=3.0/3.5`) as a config-selectable option for
the runner, and measure the arms: **(0) momentum_flip + breakeven only**, **(1) Chandelier**. Ship
at most one winner; if (0) wins, no Chandelier. (Vol-widening `m` is a later experiment that must
beat flat-m head-to-head.)

#### Step D implementation addendum (Chandelier runner trail) — 2026-06-16

Implemented as a **third arm** (`partial33_chand`) on the dual-book, identical to
`partial33_cond` except the post-partial runner trails instead of sitting at breakeven —
so the bake-off is `partial33_cond` (runner = breakeven + flip + time) vs `partial33_chand`
(runner = Chandelier). One arm, flat `m=3.0` (the common default; `3.5` is a trivial extra
arm later). No new schema — the trail reuses the existing `high_water`/`stop` and reads
`atr` off the bar (`atr` added to `run._BAR_KEYS`).

- Config: `trail_mode` (`"breakeven"` default / `"chandelier"`), `chandelier_atr_mult=3.0`.
- `advance_open`: each bar, ratchet `stop = max(stop, prior_high_water - m*ATR)` — **only
  after a partial** (`partial_done`), monotonic up, never below breakeven (the stop starts
  there). It reads the **prior** bar's `high_water` (folded forward to include this bar's
  high *after* the trail/exit check) so this bar's own high never decides this bar's stop —
  no intra-bar lookahead, and deliberately *less* optimistic than a same-bar trail given the
  shadow book's already-optimistic level fills. A non-positive/NaN ATR skips the trail that bar.

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

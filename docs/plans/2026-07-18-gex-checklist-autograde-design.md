# GEX Checklist Auto-Grader — Design

Validated with Oliver 2026-07-18 (option 1 of 3: machine pre-grade, human confirms).

**Goal:** enter a ticker in the GEX LAB checklist grader and get back which checklist
items the machine can verify, with a hard yes/no on the computable facts — while the
discipline experiment (the A+ hypothesis: *the discipline is the edge*) keeps measuring
the trader, not the machine.

## Scope: who owns which box

**Machine (8):**

| # | key | rule (thresholds in `GexConfig`, versioned into provenance) |
|---|-----|--------------------------------------------------------------|
| 1 | `chk_daily_bias_clear` | `StackState.direction` ≠ tangled AND matches setup direction |
| 2 | `chk_daily_stack_ordered` | raw EMA ordering only (9>21>50 long / reversed short) — deliberately spacing/slope-free so items 1–2 stay non-redundant |
| 3 | `chk_m5_agrees` | `compute_stack` on 5m closes: 5m ∧ daily ∧ setup direction agree (needs ≥100 bars; else `unavailable`) |
| 4 | `chk_gex_levels_marked` | same-day `GexSnapshot` with both walls non-null; **null flip is a fact, not a failure** |
| 6 | `chk_regime_match` | breakout∧negative ∨ range∧positive; `unknown` fails. Regime read from the **snapshot**; the form's free-text regime becomes pre-filled display. Requires new `play_type: breakout\|range` form/DB field |
| 8 | `chk_volume_confirming` | last **completed** 5m bar volume ≥ `vol_confirm_mult` (default 1.5) × prior-`vol_lookback` (20) bar mean AND bar body in setup direction |
| 11 | `chk_rr_at_least_2` | non-null entry/stop/target, side-sane ordering (long: stop<entry<target), ratio ≥ `rr_min` (2.0); `needs_input` until levels typed |
| 12 | `chk_confirmation_candle` | last completed 5m bar closed in setup direction; fact line names the bar time + close (deliberately simple v1 — this item separates A+ from B) |

**Human (4):** `chk_pattern_clean` (the self-honesty item — never automated),
`chk_price_at_pivot` + `chk_stop_structural` (machine renders **advisory hints**:
distance from entry/spot to nearest of {call wall, put wall, flip, manual pivot};
stop vs nearest protective level and the last-12-bar 5m swing — box stays human),
`chk_risk_sized` (deferred: no planned-risk config or size field exists, and options
risk needs an explicit premium-vs-underlying denomination decision first).

**Verdict:** any machine item fails → **NO**, failing facts named ("stack tangled ·
R:R 1.42"). All 8 pass → **"YES, pending your 4 confirms"**. Any machine item
`unavailable`/`needs_input` → **incomplete** (never fabricate a yes/no). Decision
support only — the journaled grade still comes from the full 12 via the existing
`grade()`; no execution path exists or is created (charter non-goal).

## Architecture

- **`src/swing_screener/options/autograde.py`** — pure module, lab house style
  (frozen dataclasses, injectable seams, no LLM — `reading.py`'s boundary).
  `autograde(underlying, direction, play_type, entry, stop, target, pivot_level, *,
  cfg, daily_bars, bars_5m, snapshot) -> AutoGrade` with per-item
  `ItemVerdict(key, state: pass|fail|needs_input|unavailable, fact: str)` + the two
  hints + `machine_verdict: yes|no|incomplete`.
- **`POST /api/gex/autograde`** (X-Cockpit) in `routers/gex.py`: fetches daily + 5m
  bars (intraday 5m is never cached mid-session since the 2026-07 Phase-H fix, so
  freshness is already solved) and the latest same-day snapshot — **auto-running the
  existing `run_analyze` when none exists**, so a cold ticker works in one click.
- **UI (`GexLabScreen.tsx`)**: `play_type` segmented control; **Auto-grade** button
  beside the live grade chip; machine boxes pre-filled with a distinct glyph; verdict
  line + per-item facts + the two hints; the 4 human boxes untouched. "Grade &
  journal setup" posts as today plus the machine payload.
- **Persistence:** nullable `option_setups.autograde_json` (per-item verdicts, facts,
  threshold values at decision time) — the provenance facet for later
  machine-vs-human discipline stats. The 12 `chk_*` booleans stay the submitted
  truth. Plus a hard integrity check in `create_setup`: a ticked
  `chk_rr_at_least_2` that contradicts the submitted levels is a **422**, not a
  stored lie.

## Error posture & testing

Any fetch/snapshot failure degrades that item to `unavailable — grade by eye` with
the reason; nothing fabricates, nothing 500s. Tests: pure unit tests per rule over
synthetic frames (`bias.py` test style), router tests through injected seams, the
R:R-contradiction 422, migration up/down, tsc + build + CSS brace-count for the
panel.

## Implementation tasks (one PR)

1. **T1** `autograde.py` + `GexConfig` knobs (`vol_confirm_mult`, `vol_lookback`,
   `rr_min`, `pivot_tolerance_pct`, `swing_lookback`) + unit tests per rule.
2. **T2** server wiring: endpoint + auto-analyze fallback + `play_type` on
   `SetupBody`/`option_setups` + `autograde_json` column + one alembic migration +
   `create_setup` R:R integrity 422 + provenance persistence + router tests.
3. **T3** UI: play_type control, Auto-grade button, machine glyphs, verdict/facts/
   hints rendering, api.ts types, committed static rebuild.
4. **T4** final whole-branch review → PR.

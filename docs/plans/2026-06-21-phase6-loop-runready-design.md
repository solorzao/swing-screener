# Phase 6 — deepen the learning loop + run-readiness — design

**Date:** 2026-06-21
**Governed by:** [North Star](../NORTH_STAR.md) (#1 evidence gates promotion, #2 honest evidence, #8 small/
interpretable/reversible, #9 the analyst is a learning participant whose influence *grows as it earns a
track record*) · realizes the [learning-loop design](2026-06-20-learning-loop-design.md) "needs-a-test →
draft a variant" vision + the Phase-2 "nudge bound widens as calibration proves out" deferral.
**Status:** design agreed (brainstorm 2026-06-21); not yet implemented.
**Builds on:** Phase 0-5 (all shipped + live). Preceded by a four-area grounding sweep (2026-06-21).

## Purpose

Finish the *buildable-now* work so the only remaining human jobs become **debug, monitor, and feed the
data**. Two workstreams, neither of which moves money (default execution stays `off`; the deterministic
levels + the money boundary are untouched):

- **A — deepen the learning loop** so the edge improves *faster* (→ the autonomy gate's calibration arrives
  sooner): the analyst **proposes its own screen variants** for the optimizer to test, and **earns a wider
  conviction-nudge bound** as its calibration proves out.
- **B — run-readiness** so operating is easy: make the insight engine **safe to run production-on** (cost
  guards) and add a **monitoring/health surface**.

## Decisions (brainstorm 2026-06-21)

1. **A1 — variants are queue-only, Bonferroni-counted.** The analyst freely *drafts + queues* candidate
   variants (it widens the search — its value-add); promotion into `config.py` still rides the existing
   `propose()` statistical gate + a human merge. Queued-variant counts **feed the multiple-comparisons
   correction** (non-negotiable: silently widening the search without paying for it weakens North Star #2).
2. **A2 — the nudge bound widens to ±2 only when earned, per play type, reversible.** Gated on the existing
   `conviction_calibrated` certificate (already trusted by the autonomy gate), hard-capped at `ceiling=2`,
   recomputed every run (snaps back to ±1 if the edge decays). Touches *sizing* only, never a price level.
3. **B1 — cost guard = spend logging + a hard per-run abort ceiling.** Capture + persist Opus token usage;
   accumulate $ across the digest's deep loop and fall back to deterministic text once
   `SWING_DEEP_ANALYSIS_MAX_USD` is hit. Fail-safe, symmetric with the execution money caps.
4. **B2 — health = a dashboard `System Health` page + a one-line digest health footer.** The page for
   at-a-glance; the digest line is *push* (catches a silently-dead cron).
5. **YAGNI cuts:** no multi-knob auto-promotion in `propose()` (promotion stays human-gated, single-gate
   teeth untouched); no nudge-adds-R inferential test yet (ship the calibration-gated bound first); no
   `RunLog` migration; no cheaper routine model (it would dilute the learning signal being studied).

## Workstream A — deepen the learning loop

### A1 — analyst-proposed screen variants (queue-only)

- **What a variant is.** A `StrategyConfig` delta + a name. `replay.py`/`optimize.py` already consume an
  arbitrary `{name: StrategyConfig}` grid; `variants._assert_shared_indicators` already raises if a delta
  touches a frozen indicator field (the shadow book reuses the base-built frame), so a variant may change any
  *other* knob (pullback bars, ATR mults, target params, reversal thresholds).
- **The store** (mirrors the verdicts sidecar): a frozen `ProposedVariant{name, play_type, delta: dict,
  rationale, hunch_ref, status, drafted_at, provenance}` persisted as `edge/<pt>.proposed.json`. A
  `to_config(base) -> StrategyConfig` helper runs `dataclasses.replace(base, **delta)` then
  `_assert_shared_indicators` → **an illegal/malformed delta fails loudly at draft time**, never reaching the
  grid.
- **The drafting seam.** On a `tier="hunch"` ("needs a test") verdict, the reflection's Opus author may emit
  a structured candidate (the delta keyed to the hunch's dimension/bucket + a one-line rationale), validated
  and written to the `.proposed.json` store. Code owns the schema + validation; the LLM only proposes. The
  store is revised through the existing human-gated reflection PR (it writes `edge/*`).
- **Into the sweep + honesty.** `build_config_grid` / `build_screen_variants` merge `status="queued"`
  proposed variants into the next optimizer sweep. **The queued-variant count feeds the Bonferroni `K`**
  (reflect.py family size / the propose gate), so the widened search is paid for. Promotion to `config.py`
  stays the existing `propose()` gate + a human merge.

### A2 — auto-widening the conviction-nudge bound

- **The clamp today** is a hard `±1` literal in `_clamp_conviction` (analysis.py). Make it a parameter:
  `analyze_conviction(..., max_step: int = 1)` threaded into `_parse_conviction` → `_clamp_conviction`.
  **Default 1 → byte-identical behavior; every existing test passes unchanged.**
- **The gate.** A pure `max_conviction_step(calib: CalibrationVerdict, *, ceiling: int = 2) -> int` returns
  `2` only when `calib.calibrated` is True, else `1`; hard-capped at the named `ceiling=2`. Computed
  **per play type** (continuation may certify while reversal hasn't) from `conviction_calibrated`
  (calibration.py — the autonomy gate's trusted clustered test), in the digest deep path, and passed into
  `analyze_conviction`.
- **Honest + reversible:** recomputed every run from the live scored book → if the edge decays, the bound
  snaps back to ±1 next digest (no ratchet). It widens *sizing influence* (the `_MULT` rung), never a level.

## Workstream B — run-readiness

### B1 — insight engine production-on safely

- **Spend logging.** Capture `resp.usage` (input/output tokens) at the analysis seam (it's currently
  discarded), surface it on the `ConvictionResult`/`SignalAnalysis`, and persist `input_tokens`,
  `output_tokens`, `web_searches`, and an `est_cost_usd` on `AnalystCall` (a small migration). Cheap;
  measure-before-acting.
- **Per-run abort ceiling.** A `SWING_DEEP_ANALYSIS_MAX_USD` setting (default unset = no ceiling, preserving
  today). In `send_digest`'s deep loop, accumulate the estimated $ per `_deep_one`; once the ceiling is
  reached, **stop running the deep analyst and fall back to deterministic text** (`analyze_signal`) for the
  rest, logging the cutoff. Fail-safe; mirrors the execution `max_daily_*` pattern.

### B2 — monitoring / health surface

- **A `System Health` dashboard page** (new entry in the `PAGES` registry; no new schema): (i) a per-job
  last-run table from `repo.latest_run_date` + `repo.list_email_log` with a red/green **stale badge**;
  (ii) the **gate countdown** (`autonomy_gate` + `gate_countdown`); (iii) an **execution-mode** chip
  (`load_settings`); optionally the day's deep-analysis spend (from B1). Read-only.
- **A one-line digest health footer** (push): a status line in the daily digest — e.g. last screen run +
  freshness + the execution mode — so a silently-dead cron is visible without opening the dashboard. Reuses
  the existing footer plumbing (the gate status line seam).

## Data flow

1. **Reflection run** → on a hunch, the author drafts a validated `ProposedVariant` into `edge/<pt>.proposed.json`
   (human-gated PR). 2. **Weekly optimizer** merges queued variants into the sweep (count feeds Bonferroni);
   a winner → the existing human-gated `propose()` PR. 3. **Digest deep path** computes `max_conviction_step`
   per play type from the live calibration → the analyst may nudge ±2 where earned; each deep call's token
   spend is logged to `AnalystCall` and bounded by the per-run ceiling. 4. **Operator** reads the System
   Health page + the digest health line; the autonomy countdown shows the gate approaching.

## Boundary, error handling, testing

- **Boundary (North Star #1/#2/#9):** nothing moves money (default `off`, untouched); levels stay
  deterministic; promotion stays human-gated + statistically certified (the analyst only *queues* variants
  and only *earns* a bounded sizing nudge); the widened search is paid for in the correction.
- **Error handling:** an illegal proposed-variant delta fails at draft/validation (never poisons the grid);
  the nudge default (`max_step=1`) and the cost-ceiling default (unset) preserve today's behavior exactly;
  the cost ceiling degrades to deterministic text (never blocks the digest); the health surfaces are
  read-only and degrade gracefully.
- **Testing:** pure where possible — `to_config` validation (legal vs illegal delta), `max_conviction_step`
  (calibrated→2 / not→1 / cap), the spend-accounting + the ceiling abort, the grid-merge + Bonferroni count,
  the health-page rendering (AppTest) + the digest health line. No network (LLM + the author seam mocked).

## Build order (TDD, smallest/safest first)

1. **B1a** spend logging (no behavior change). 2. **B1b** per-run spend ceiling. 3. **A2** parameterize the
clamp + the calibration-gated step. 4. **B2** System Health page + digest health line. 5. **A1** the
`ProposedVariant` schema + `to_config` validation + the Bonferroni accounting + the grid merge, **then** the
LLM drafting seam (the single riskiest piece — land the validated store + accounting before the LLM emits).

## Out of scope (later)

Multi-knob auto-promotion in `propose()`; the nudge-adds-R inferential test; a `RunLog` error feed +
persisted failure history; a cheaper routine analyst model; the execution polish (Robinhood review→place MCP,
partial/fractional fills, native brackets — premature until arming); the real-money arming flip (a human act
once the gate passes). Auto-arming is never in scope.

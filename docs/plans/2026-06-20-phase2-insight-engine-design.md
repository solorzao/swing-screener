# Phase 2 — the insight engine (a learning analyst) — design

**Date:** 2026-06-20
**Governed by:** [North Star](../NORTH_STAR.md) (esp. the refined #1 + new #9) · refines Phase 2 of the
[learning-loop design](2026-06-20-learning-loop-design.md). Builds on Phase-0 (honest stats, #41) and
Phase-1 (the edge-file playbooks + deterministic grader, #43).
**Status:** design agreed (brainstorm 2026-06-20); not yet implemented.

## Purpose

Turn the deep-analysis LLM from a narrator into a **learning analyst** that, per candidate, tells me
where the trade sits versus the strategy's playbook, **forms its own judgment** (a conviction it can
move, with a reason), layers the external context the stats can't see, and hands me a concrete,
conviction-scaled **order intent** — and whose own calls are logged and scored against outcomes so its
judgment earns a track record over time. This is the decision-support piece that removes emotion from
each pick. Execution stays manual (Phase 3 adds adapters).

## Decisions (brainstorm 2026-06-20)

1. **The analyst is a learning participant (North Star #9), not a data-narrator.** The stats are the
   floor it reasons from, not a cage.
2. **Conviction = C (baseline + nudge).** Code computes a *baseline* grade from where the pick sits in
   the edge-file playbook; the LLM then **moves it** (up or down) with a stated reason, and supplies the
   qualitative why + external context. Its judgment counts; the baseline anchors it to evidence.
3. **Learning loop = A (log + score).** Every analyst call (baseline, final conviction, the nudge +
   reasoning, qualitative notes) is persisted, keyed to the pick; as that pick's shadow-book trade
   resolves, the realized R is attributed back — the analyst gets its OWN conviction calibration. The
   Phase-1 reflection reviews it: recurring proven observations get promoted in the playbook, empty
   confidence is surfaced, and the analyst's influence grows as it earns trust.

## Components

- **Insight engine** — evolve `notify/analysis.py`'s deep path. Inputs per candidate: the strategy's
  `edge/<play_type>.md` playbook, the pick's conditions (score-band + `volatility_tier` from the
  Signal; **current regime** via `classify_regime` on live SPY at analysis time), the chart, and
  external context (fundamentals/news/`web_search`). Output: an `InsightResult` carrying the rendered
  insight + a conviction-graded `OrderIntent`.
- **Baseline conviction (pure, deterministic)** — a `conviction_baseline(play_type, conditions, edge_file)`
  that maps the pick's buckets to playbook verdicts: matches a **forward-confirmed** edge (and no
  falsified) → `high`; a **falsified** pattern → `avoid`; a **screened** candidate → `medium`/`low`;
  nothing notable → `medium`. Pure + tested.
- **The LLM nudge** — the analyst returns a final conviction + a one-line reason; bounded to ±1 grade
  from the baseline *for now* (it has not yet earned more; the bound widens as its calibration proves
  out — a later refinement). The reason is logged. On any LLM failure → the deterministic baseline (and
  the existing deterministic rationale), so a pick always gets a conviction.
- **Sizing (pure)** — `size_order(conviction, entry_ceiling, stop, risk_unit)`: `shares =
  (risk_unit$ × conviction_mult) / (entry_ceiling − stop)`, capped at a configured max; `conviction_mult`
  = high 1.0 / med 0.5 / low 0.25 / avoid 0. `risk_unit$` from config (default **1% of equity** via
  `SWING_ACCOUNT_EQUITY`, or a fixed `SWING_RISK_PER_TRADE_$`); missing config → R-multiples only
  (account-agnostic), never a guessed dollar amount.
- **`OrderIntent`** — `{ticker, side, entry_floor/ceiling, conviction, shares, risk_$, stop, target,
  edge_played, key_risk, insight}`. Rendered in the digest + dashboard; consumed by a Phase-3 adapter
  later. Levels are the deterministic ground truth, never set here.
- **`AnalystCall` store (new table)** — persists each call: pick keys (`ticker, timeframe, play_type,
  run_date`), `baseline_conviction`, `final_conviction`, `nudge_reason`, model, plus `realized_r` /
  `scored_at` filled in later when the matching shadow trade closes. A migration (nullable columns).

## Data flow

1. Digest/on-demand selects a candidate → insight engine: baseline conviction (playbook) → Opus call
   (nudge + reason + insight + external context) → `OrderIntent` → rendered for me. The `AnalystCall`
   row is written at analysis time (unscored).
2. The candidate's shadow-book trade resolves → a scoring step joins the `AnalystCall` to the paper
   trade's `realized_r` (join on `ticker, timeframe, play_type` + the fill-date offset; this needs the
   pick→fill link — see open questions) and stamps the call's outcome.
3. The Phase-1 reflection reads the scored calls → the analyst's **conviction calibration** (does its
   `high` out-earn its `low`? do its nudges add R over the baseline?) → promotes proven recurring
   observations into the playbook, surfaces empty confidence.

## Boundary, error handling, testing

- **Boundary:** levels are deterministic ground truth; the analyst advises conviction/sizing/take-skip
  and never moves a level. Money never auto-moves (North Star #1) — this is decision-support; execution
  is manual until Phase 3.
- **Error handling:** any LLM failure → deterministic baseline conviction + deterministic rationale +
  R-multiple sizing (no block), mirroring `notify/analysis.py`. Missing risk config → R-multiples only.
- **Testing:** `conviction_baseline` + `size_order` are pure and deterministically tested (bucket→grade,
  shares math, the cap, missing-config fallback). The LLM nudge is a mockable seam (fake client →
  canned conviction+reason; raise → baseline) — no network in tests. The `AnalystCall` write + the
  scoring join are tested against an in-memory DB.

## Open questions (resolve in the plan)

- **The pick→outcome join for scoring** — the analyst call is on a Signal; the shadow book fills the
  prior-bar signal on the next bar. Need a persisted link (thread `signal_id` through `FillCandidate` —
  the long-noted None-join fix) or a documented `(ticker, timeframe, play_type, run_date+1)` convention.
- `risk_unit` config form + default (equity % vs fixed $); how the cap is set.
- The exact baseline rubric thresholds (which bucket combination → which grade) and the nudge bound
  (±1 now; how/when it widens as calibration proves out).
- Whether the conviction calibration eventually feeds the nudge bound automatically (later).
- Surfacing detail: digest deep path (per top pick) + on-demand per ticker — both reuse existing seams.

## Out of scope (later phases)

Execution adapters (manual/paper/robinhood) + autonomy (Phase 3); commissioned tests / the analyst
proposing its own variants (Phase 4); auto-widening the nudge bound from the calibration.

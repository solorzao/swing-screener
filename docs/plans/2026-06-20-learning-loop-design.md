# Learning Loop — design (insight engine + per-strategy edge files)

**Date:** 2026-06-20
**Governed by:** [North Star](../NORTH_STAR.md). Every choice below traces to a principle there;
if a future change conflicts with the North Star, change the change.
**Status:** design agreed (brainstorm 2026-06-20); not yet implemented.

## 1. Purpose & what this adds

The repo already has the **quantitative** edge-finding loop (#37/#38): replay →
significance-ranked leaderboard → walk-forward optimizer → human-gated config proposal. This
design adds the **qualitative** loop — the trader's *understanding* — and couples the two:

- A per-pick **insight engine** that tells me where *this* trade sits versus the strategy's
  known edges and failure modes, with a graded conviction and a concrete order intent.
- An event-driven **reflection** pass that maintains a per-strategy **edge file** — a living,
  evidence-graded playbook.
- A **hunch → confirmed-edge** lifecycle graded by the loop's *hardened* statistics.

The two loops share the edge files; I stay the gate until autonomy is earned. This is the
"observe → hypothesize → test → adjust → re-evaluate" loop of a disciplined trader, made systemic.

## 2. Load-bearing dependency: the statistics must be honest first

Per North Star principle #1, a hunch becomes a "confirmed edge" only when the statistics say
so — so the edge files are only as trustworthy as the inference underneath. The current loop
optimizes a **gross optimistic-fill upper bound** certified by an **IID normal-approximation
CI** with **no multiple-comparisons control** (verified: `shadow.py` books stops/targets/partials
at the exact level; `performance.py` uses `stdev/sqrt(n)`; `propose.py` compares OOS point
estimates; `optimize.py` picks best-of-N uncorrected). **Phase 0 hardens this and ships first.**
Everything qualitative is built on top of it.

## 3. Architecture

```
                 ┌─────────────────── edge/<strategy>.md (shared playbook) ───────────────────┐
                 │  thesis · confirmed edges (n,CI,net-of-cost) · hunches/needs-test ·         │
                 │  falsified/retired · open questions     [git-versioned = the journal]       │
                 └───────▲───────────────────────────────────────────────────▲────────────────┘
        reads (per pick) │                                                     │ writes (event-driven)
                 ┌───────┴────────┐                                   ┌────────┴─────────┐
                 │ INSIGHT ENGINE │  per candidate                    │  REFLECTION PASS │  per N new closed trades
                 │ (Opus)         │                                   │  (Opus)          │
                 │ → OrderIntent  │                                   │ → revised edge   │
                 └───────┬────────┘                                   │   file (PR)      │
                         │ order intent                               └────────┬─────────┘
                 ┌───────▼────────┐                                            │ grades hunches against …
                 │ EXEC ADAPTER   │  manual | paper | robinhood(later)         │
                 └────────────────┘                                   ┌────────▼─────────┐
                                                                      │ QUANT LOOP (#37/#38) │
                                                                      │ leaderboard · regime │
                                                                      │ score calib · optimizer │
                                                                      └──────────────────────┘
```

The insight engine and reflection are two prompts over the SAME edge files. The quant loop is
the grader. The human gates every config change and (until earned) every trade.

## 4. Components

### 4.1 Edge files — `edge/<strategy>.md` (one per play type: `continuation`, `reversal`)
Plain markdown, committed, hand-editable. Sections: **Thesis**; **Confirmed edges** (each: the
condition, measured effect, `n`, 95% CI, *net-of-cost*, regime/scope, date confirmed);
**Hunches / needs-a-test** (hypothesis, the breakdown/variant that would confirm it, status);
**Falsified / retired** (tested and disproven — so the agent stops re-suggesting and I don't
relearn it); **Open questions / watchlist**. Per-play-type only — continuation and reversal
scores are non-comparable scales and reversals have systematically-missing features
(`extension_atr`/`mtf`), so they are never pooled.

### 4.2 Reflection pass (the learning step)
Trigger: **event-driven** — fires when a play type accumulates ≥ N new closed paper trades
since its last reflection (N tied to the significance machinery; tracked via a small state
marker). On fire, for that strategy it assembles: the prior edge file, the closed-trade
outcomes, and the **hardened** quant breakdowns (by regime, score band, variant — with
clustered CIs, net-of-cost), then one Opus call revises the edge file:
- **Hunch → confirmed edge** iff a covering breakdown clears the hardened gate (n ≥ threshold,
  clustered `ci_low > 0`, net-of-cost). **Hybrid grading:** if no existing breakdown covers the
  hunch, mark it *needs-a-test* and draft a candidate **variant** (a `StrategyConfig` diff within
  the variant constraints) for the optimizer/me to run.
- **Confirmed/​hunch → falsified** if the evidence now contradicts it.
Output is a revised edge file opened as a **human-gated PR** (the `optimize.yml` pattern). Nothing
auto-merges.

### 4.3 Insight engine (per pick) — evolves `notify/analysis.py`
Today's analyst *narrates*; this makes it *reason*. Inputs: the pick's deterministic facts +
levels, its features (regime, score band, `extension_atr`, tags, `play_type`), the strategy's
**edge file**, and external context (fundamentals/news/`web_search`). Output — a structured
**`OrderIntent`** plus a rendered insight:
- **Conviction** `high | medium | low | avoid`, derived from which edge-file bucket the setup
  maps to (confirmed-edge zone → high; near a falsified pattern → avoid; an unconfirmed hunch →
  low/"probe"; otherwise medium). Evidence-tied, never vibes.
- **The insight** — where this sits vs. the playbook + the external context that bears on the
  thesis + the single biggest risk. Not a summary.
- **Sizing** — concrete, conviction-scaled, R-based: `shares = (risk_unit$ × conviction_mult) /
  (entry_ceiling − stop)`, capped at a configured max; `risk_unit$` from a local config
  (e.g. 1% of equity or a fixed $). `conviction_mult`: high 1.0 / medium 0.5 / low 0.25 / avoid 0.
- **Boundary:** levels stay deterministic ground truth — the engine annotates, sizes, and
  advises take/skip; it never moves a level. Deterministic fallback on any LLM failure.
- **Cost:** gated as today (default-off / top-N). Opus is justified by insight, not summary.

### 4.4 Order intent + execution adapter
The engine emits `OrderIntent{ticker, side, entry zone/limit, shares, stop, target, conviction,
edge_played, key_risk}`. A pluggable adapter consumes it: **`manual`** (render for me + record
in the shadow book) and **`paper`** (dry-run against the shadow book) now; **`robinhood`**
(agentic/MCP) when autonomy is earned. The intent is fully specified so the future is an adapter
swap, not a rewrite.

## 5. Data flow

1. Nightly screen → candidates (unchanged). On request, the **insight engine** reads the edge
   file + context → `OrderIntent` → adapter (`manual`/`paper`) → shadow book.
2. Shadow book accrues closed trades; the quant loop computes hardened breakdowns.
3. When a play type crosses N new closed trades → **reflection** revises its edge file (PR).
4. Confirmed edges inform future insights and may seed config proposals; *needs-a-test* hunches
   become candidate variants the optimizer runs. Loop repeats.

## 6. Error handling & safety

- LLM failure (insight or reflection) → deterministic fallback / no-op PR; the pipeline never
  blocks (existing `analysis.py` pattern).
- Missing `risk_unit` config → fall back to R-multiple sizing (account-agnostic), never guess $.
- Reflection only ever *opens a PR*; it cannot change levels, config, or the edge file without my
  merge. Autonomy (real execution) is gated LAST behind: confirmed edges under hardened stats,
  **conviction calibration** (does "high" out-earn "low"? — a `score_bucket` analogue for
  conviction), hard risk limits (max risk/trade, max concurrent, max daily loss), and a kill
  switch; it only ever fires on confirmed-edge setups.

## 7. Testing

- Phase 0: no-lookahead spike-fixture + golden-master tests on `replay`/`optimize`; the
  clustered bootstrap, haircut (byte-identical at 0.0), and delta/placebo gate are pure + tested.
- Grading rules, conviction mapping, and sizing math are pure functions tested deterministically
  (given breakdowns + a hunch → expected promote/demote/falsify; given facts + conviction →
  expected shares). LLM seams are mocked (no network in tests). Adapters tested against the
  paper book.

## 8. Phased sequencing

- **Phase 0 — Hardening (foundation, ships first):** no-lookahead/golden-master tests → fill-cost
  haircut → ticker-clustered bootstrap `ci_low` → `propose()` delta + placebo + distinct-ticker
  gate + provenance. Makes every downstream number honest.
- **Phase 1 — Edge files + reflection (read-only grading):** the file format, the event trigger,
  the reflection prompt, the hybrid grading (existing breakdowns only), human-gated PR.
- **Phase 2 — Insight engine (C):** evolve `analysis.py` to emit `OrderIntent` with conviction +
  concrete sizing, grounded in the edge file.
- **Phase 3 — Execution arc + conviction calibration:** the adapter interface, `manual`/`paper`
  adapters, conviction-outcome tracking.
- **Phase 4 — Commissioned tests + autonomy (horizon):** the agent drafts/runs its own variants;
  the `robinhood` agentic/MCP adapter; autonomy behind the full gate.

## 9. Open questions (resolve before/within each phase)

- The reflection trigger N (tie to significance / minimum new closed trades per play type).
- `risk_unit` config form: % of equity (needs an equity input) vs. fixed $ (simplest). Default?
- Exact conviction rubric thresholds (which score/regime/edge combination → which grade).
- Whether confirmed edges should *auto-draft* a config proposal (human-gated) or stay advisory.
- Translating an LLM hunch into a safe, config-expressible variant (the *needs-a-test* on-ramp).
- Robinhood agentic-trading + MCP capabilities — verify at Phase 4, design to the adapter
  interface until then.

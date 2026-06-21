# Phase 1 — edge files + event-driven reflection — design

**Date:** 2026-06-20
**Governed by:** [North Star](../NORTH_STAR.md) · refines Phase 1 of the
[learning-loop design](2026-06-20-learning-loop-design.md). Builds directly on the
Phase-0 hardened statistics (#41), which are the grader.
**Status:** design agreed (brainstorm 2026-06-20); not yet implemented.

## Purpose

The first piece of the **qualitative** loop: a per-strategy, evidence-graded **playbook**
the system maintains as it learns — the artifact the per-pick insight engine (Phase 2) will
later read. This phase builds the playbook and the **reflection** pass that keeps it current.
Read-only grading: the reflection observes what the existing measurement already computes; it
does not yet commission its own tests (that is Phase 4).

## Decisions (brainstorm 2026-06-20)

1. **Deterministic grading, LLM writes (North Star #1).** *Code* computes every
   confirmed/screened/falsified verdict from the Phase-0 hardened breakdowns. The LLM only
   *authors the prose* around those verdicts and *drafts hypotheses* — it can never stamp a
   claim the math didn't.
2. **Tiered evidence.** A claim is **replay-screened** (a backtest-supported candidate) when
   the haircut replay corpus clears the gate, and only **forward-confirmed** (the gold
   standard) when the *live forward shadow book* independently clears it. Both tiers ride the
   Phase-0 hardened inference; the file shows them distinctly. (Backtests screen ideas; live
   confirms them.)
3. **Small pre-registered univariate family, MC-aware.** Per play type, grade a *fixed,
   checked-in* short list of conditions — `market_trend`, `score` band, `volatility_tier` —
   with the family size carried so each verdict clears a best-of-K multiple-comparisons
   correction. No cross-products (combinations explode the family and need far more data).
4. **Trigger:** event-driven — reflect a play type when it accrues **≥ 20 new closed forward
   trades** (= `MIN_LEADERBOARD_N`) since its last reflection. The replay-screened tier is
   recomputed each fire (deterministic over the cache).
5. **Seed:** hand-write each file's **thesis** once (the strategy's core idea) + empty
   sections, committed; reflection populates the rest.

## Architecture & components

- **`edge/continuation.md`, `edge/reversal.md`** — playbooks. Sections: **Thesis** ·
  **Confirmed edges** (forward-confirmed; each: condition, effect, n, clustered 95% CI,
  net-of-cost, family size, date) · **Screened candidates** (replay-screened, same stats,
  marked screen-not-proof) · **Hunches / needs-a-test** · **Falsified / retired** · **Open
  questions**. Git-versioned (the history is the journal), hand-editable.
- **`pipeline/reflect.py`** — two clean halves:
  - *Deterministic grading* (pure, tested): `grade(play_type, forward_trades, replay_trades,
    family) -> list[Verdict]`. For each pre-registered dimension+bucket it summarizes (reusing
    `analytics.performance.summarize`/`breakdown` + the clustered `expectancy_ci_low`), applies
    the MC-aware bar over the known family size, and emits a tier (forward-confirmed /
    replay-screened / hunch) or a falsification of a prior claim.
  - *LLM authoring* (mockable seam, like `notify/analysis.py`): given the verdicts + the prior
    edge file + outcome context, Opus writes the prose file and drafts "needs-a-test"
    hypotheses for ideas no graded bucket covers. **Deterministic fallback:** on any LLM
    failure, a template renders the file from the verdicts alone, so reflection never blocks.
- **CLI / job** — fires the reflection per due play type and opens a **human-gated PR** that
  edits the `edge/*.md` files (the `optimize.yml` pattern). Nothing auto-merges.

## Data flow

1. The forward shadow book accrues closed trades (existing). A small **state marker** tracks,
   per play type, how many closed forward trades existed at the last reflection.
2. When (current − last) ≥ 20 for a play type → reflection fires for it.
3. Grading runs over (a) the live forward breakdowns and (b) the haircut replay corpus
   (`replay_book`, Phase 0), producing tiered verdicts.
4. The LLM authors the revised `edge/<play_type>.md`; a PR opens for human review/merge.
5. Confirmed/screened edges and drafted hypotheses become the playbook the Phase-2 insight
   engine reads.

## Boundary, error handling, testing

- **Boundary (North Star #4):** the edge files are documents; nothing here reads or moves
  entry/stop/target. The reflection only ever opens a PR — it cannot change config, levels, or
  the files without a human merge.
- **Error handling:** LLM failure → deterministic template file (no block); a play type below
  the trigger → skipped; missing replay cache → the screened tier is simply empty (forward
  tier still graded).
- **Testing:** the grader is pure and tested deterministically — given fixed forward/replay
  breakdowns + the family, assert the exact tier per bucket *including* the MC correction (a
  bucket that clears the naive bar but not the best-of-K bar must NOT be stamped); falsification
  flips a prior claim; the LLM seam is mocked; the template fallback is tested.

## Open questions / deferred

- Exact MC correction form for K univariate buckets (Bonferroni on the family vs an
  effective-N/permutation null) — pick the simplest defensible one in the plan; keep K small.
- The grading dimension list is pre-registered in code — adding a dimension is a deliberate,
  git-visible change (resets the family size).
- Whether a forward-confirmed edge should *auto-draft* a config proposal (human-gated) — left
  to a later phase; this phase is read-only.
- Commissioned tests (the agent proposing its own variants for uncovered hunches) = Phase 4.
- Combinations / cross-products of conditions = deferred (overfitting risk + data hunger).

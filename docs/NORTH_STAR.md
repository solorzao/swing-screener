# Swing-Screener — North Star

> The purpose and destination of this project. **Every design and implementation decision is
> checked against this document.** When a choice doesn't serve the Purpose or violates a
> Guiding Principle, it's the wrong choice — change the choice, not the North Star.

## Purpose

Remove emotional risk from my trading by building a **systemic, evidence-based edge** — and
find and grow that edge the way a disciplined trader actually reaches profitability: observe,
hypothesize, test, adjust, and re-evaluate, continuously.

The system is the discipline I can't reliably supply in the moment.

## Destination

A self-improving trading system with **two coupled edge-finding loops** — quantitative
measurement and qualitative insight — sharing one living, evidence-graded **playbook per
strategy**. It ultimately **executes autonomously**, but only edges it has *proven*, sized by a
rule that never bends to emotion, under hard limits I can always override.

## Guiding principles — every decision is checked against these

1. **Evidence over narrative.** A "hunch" becomes a "confirmed edge" only when the hardened
   statistics say so. The LLM's — or my — confidence is never the grader.
2. **Honest about uncertainty.** Every claim carries its sample size, a confidence interval,
   and is *net of costs*. A biased or optimistic number is never shown as fact.
3. **The human gate, until proven.** Nothing ships or executes without proof. Autonomy is
   earned incrementally, gated, and always reversible (kill switch).
4. **Deterministic levels are ground truth.** The rules engine sets entry / stop / target.
   Models and the LLM may annotate, size, and advise take/skip — they never move a level.
5. **Mirror the trader's loop.** Observe → hypothesize → test → adjust → re-evaluate. The
   playbook's git history is the trading journal.
6. **Remove discretion where emotion leaks in.** Concrete, conviction-scaled sizing and
   (eventually) execution, so there is no in-the-moment math, hesitation, or override.
7. **Measure only what I'd actually trade.** A gate filters both what's surfaced *and* what's
   forward-tested, so the leaderboard reflects real entries.
8. **Small, interpretable, reversible.** Prefer the simplest change that closes the loop.
   Widen the search space only behind stronger guards.

## How it works

- **Quantitative loop (exists):** replay → significance-ranked leaderboard → walk-forward
  optimizer → human-gated config proposal.
- **Qualitative loop (building):** an Opus *insight engine* — per pick: where this trade sits
  vs. the strategy's known edges and failure modes, the external context that bears on the
  thesis, a graded conviction, and a concrete, conviction-scaled order intent — plus an
  *event-driven reflection* pass that maintains the per-strategy edge files.
- **Shared playbook:** `edge/<strategy>.md` — thesis, confirmed edges, hunches / needs-a-test,
  falsified / retired, open questions. Versioned, human-readable, hand-editable.
- **Execution arc:** the engine emits a structured **order intent**; a pluggable adapter runs
  it — `manual` and `paper` now, `robinhood` (agentic / MCP) when autonomy is earned.

## Success looks like

- **Near:** the loop's numbers are honest (net-of-cost, clustered CIs, tested deltas); the
  playbook learns from real evidence; I make calmer take/skip/size calls with zero
  in-the-moment math.
- **Mid:** confirmed edges accumulate; conviction grades are *calibrated* (high out-earns low);
  paper-execution proves the end-to-end order flow before a dollar is risked.
- **Far:** autonomous execution of confirmed-edge setups via the Robinhood / MCP adapter, under
  hard risk limits + a kill switch — emotion fully removed from the loop.

## Non-goals / anti-patterns

- Not day-trading — swing horizons only.
- No auto-deploying or auto-executing an unproven or overfit edge.
- No LLM- or model-set price levels.
- No optimizing on, or presenting, gross / biased numbers as if they were real.
- No widening the search space (more knobs, ML) before the inference that judges it is
  trustworthy.
- No black box: everything versioned, auditable, reversible.

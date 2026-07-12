# Meridian — North Star

> The purpose and destination of the suite. **Every design and implementation decision, in
> every module, is checked against this document.** When a choice doesn't serve the Purpose or
> violates a Guiding Principle, it's the wrong choice — change the choice, not the North Star.
>
> Meridian is a **suite of interchangeable strategy modules on permanent shared
> infrastructure**. This document is the constitution for all of it. Each module also has its
> own charter (scope, horizon, instruments — see [Modules](#modules)); charters rule their
> module's scope but may never contradict a principle here. Structure and engineering
> contracts live in [ARCHITECTURE.md](ARCHITECTURE.md). *(The repo/package keeps the
> `swing-screener` name until a rename earns its cost — identity lives here, not in the
> import path.)*

## Purpose

Remove emotional risk from my trading by building a **systemic, evidence-based edge** — and
find and grow that edge the way a disciplined trader actually reaches profitability: observe,
hypothesize, test, adjust, and re-evaluate, continuously.

The system is the discipline I can't reliably supply in the moment. Strategies will come and
go — modules are interchangeable. What is permanent is the machinery that observes every
trade, keeps the P&L honest, journals every decision, and turns settled outcomes into insight.

## Destination

A self-improving trading suite where every module runs **two coupled edge-finding loops** —
quantitative measurement and qualitative insight — sharing one living, evidence-graded
**playbook per strategy** and one suite-wide **journal** of every trade, thesis, and outcome.
It ultimately **executes autonomously**, but only edges it has *proven*, sized by a rule that
never bends to emotion, under hard limits I can always override.

## Guiding principles — every decision, in every module, is checked against these

1. **Evidence gates promotion, not judgment.** A "hunch" becomes a "confirmed edge" — and
   anything that auto-changes config or moves real money — only when the hardened statistics
   say so; *there*, the LLM's (or my) confidence is never the grader. This guardrail is scoped
   to the promotion / execution boundary. It does NOT silence the analyst's judgment in
   decision-support (see #9) — the stats are the floor it reasons from, not a cage.
2. **Honest about uncertainty.** Every claim carries its sample size, a confidence interval,
   and is *net of costs*. A biased or optimistic number is never shown as fact. Statistics
   cluster on each module's declared independence unit, and books never pool across modules
   or units — comparisons are labeled, inference is per-book.
3. **The human gate, until proven.** Nothing ships or executes without proof. Autonomy is
   earned incrementally, gated, and always reversible (kill switch).
4. **Deterministic levels are ground truth.** The rules engine sets entry / stop / target.
   Models and the LLM may annotate, size, and advise take/skip — they never move a level.
5. **Mirror the trader's loop.** Observe → hypothesize → test → adjust → re-evaluate. The
   playbooks' git history and the journal layer — theses captured at time-of-knowledge, with
   provenance on every annotation — are the trading journal.
6. **Remove discretion where emotion leaks in.** Concrete, conviction-scaled sizing and
   (eventually) execution, so there is no in-the-moment math, hesitation, or override.
7. **Measure only what I'd actually trade.** A gate filters both what's surfaced *and* what's
   forward-tested, so the leaderboard reflects real entries.
8. **Small, interpretable, reversible.** Prefer the simplest change that closes the loop.
   Widen the search space only behind stronger guards.
9. **The analyst is a learning participant, not a narrator.** In decision-support (human-gated,
   no money auto-moves), the LLM analyst has genuine input — it forms and evolves its own
   per-strategy understanding, *moves* the conviction grade with a stated reason, and draws
   qualitative connections the stats can't. The discipline is not muzzling it: every call is
   logged and scored against the realized outcome, so its judgment earns a track record and
   more influence as it proves out — the way a trader's intuition matures into a validated edge.
   It's not all about the data.

## How it works

- **The platform** ([ARCHITECTURE.md](ARCHITECTURE.md)): data seams, one database and
  migration chain, the evidence/learning machinery, the journal & observability layer, the
  desktop cockpit, the ops chassis, and (future) the execution layer. Permanent; module-agnostic.
- **Per module, two loops:** a **quantitative loop** (replay / forward books → significance-
  ranked stats → optimizer → human-gated config proposal) and a **qualitative loop** (the
  Opus insight engine + event-driven reflection maintaining the per-strategy edge files).
- **Shared playbooks:** `edge/<strategy>.md` — thesis, confirmed edges, hunches / needs-a-test,
  falsified / retired, open questions. Versioned, human-readable, hand-editable.
- **The journal layer** ([design](plans/2026-07-11-journal-layer-design.md)): observability of
  all trades across all books — P&L, entry/exit theses, tags and mistakes with provenance,
  discipline metrics, and the session-review coach.
- **Execution arc:** a module's engine emits a structured **order intent**; a pluggable adapter
  runs it — `manual` and `paper` now, **`alpaca` live** when autonomy is earned. (Decision
  2026-06-21: a headless-auth spike found Robinhood's agentic MCP unusable for an unattended
  cron — desktop-per-session OAuth, no paper sandbox — so Alpaca is the autonomous broker and
  Robinhood remains a human-approval surface via the `manual` adapter's order tickets.)

## Modules

| # | Module | Charter | Horizon | Status |
|---|--------|---------|---------|--------|
| 1 | Swing screener | [modules/swing-screener.md](modules/swing-screener.md) | days–weeks | live in production |
| 2 | GEX options lab | [modules/gex-lab.md](modules/gex-lab.md) | intraday | Phase 1 in build |

A module packages one or more related strategies behind one charter, one config, its own
tables and books, and its own edge files. The [module contract](ARCHITECTURE.md) defines what
every module must bring.

## Success looks like

- **Near:** every loop's numbers are honest (net-of-cost, clustered CIs, tested deltas); the
  playbooks and journal learn from real evidence; I make calmer take/skip/size calls with zero
  in-the-moment math.
- **Mid:** confirmed edges accumulate across modules; conviction grades are *calibrated* (high
  out-earns low); paper-execution proves the end-to-end order flow before a dollar is risked;
  the journal shows discipline improving, not just P&L.
- **Far:** autonomous execution of confirmed-edge setups via the Alpaca adapter, under hard
  risk limits + a kill switch — emotion fully removed from the loop.

## Non-goals / anti-patterns

- No auto-deploying or auto-executing an unproven or overfit edge.
- No LLM- or model-set price levels.
- No optimizing on, or presenting, gross / biased numbers as if they were real.
- No widening a module's search space (more knobs, ML) before the inference that judges it is
  trustworthy.
- No pooling books, modules, or units in inference — and no unit-mixing in displays.
- No black box: everything versioned, auditable, reversible.
- Single-operator: no SaaS scaffolding, no multi-user, no product tiers.

*(Horizon restrictions are module scope, not suite law: "swing horizons only" lives in the
swing screener's charter; the GEX lab is deliberately intraday under its own charter.)*

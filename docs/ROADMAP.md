# Meridian — Roadmap

**Updated:** 2026-07-11. The agreed sequence and, for each phase, the **trigger** that starts
it and the **planning artifact** it needs before code. One deliberate rule, learned from this
repo's own history: **implementation plans are written right before execution**, grounded in a
fact-extraction pass over the code as it exists then — plans written far ahead of their
substrate rot. So later phases are *scoped* here, not task-planned; that's a feature.

Governance: [NORTH_STAR.md](NORTH_STAR.md) (constitution) ·
[ARCHITECTURE.md](ARCHITECTURE.md) (module contract, extraction policy) ·
charters in [modules/](modules/).

| # | Phase | Scope (one line) | Trigger to start | Planning state |
|---|-------|------------------|------------------|----------------|
| 1 | **GEX lab Phase 1** | Morning GEX map + day plan, cockpit checklist grader + journal, nightly 5m settlement, Robinhood import with review-and-tag, Meridian wordmark | **Ready now** — worktree `gex-lab-phase1`, branch `feat/gex-options-lab` | ✅ [Full 19-task plan](plans/2026-07-11-gex-lab-phase1-implementation.md), audit-hardened |
| 2 | **GEX lab Phase 1.5 — Half A** | Setup↔trade linking (planned-vs-realized), journal completion (manual close, tag editing, needs-review queue), by-regime / per-item stats, first `scope='gex'` experiments | Phase 1 merged | Scoped in the [design doc §Phase 1.5](plans/2026-07-11-gex-options-lab-design.md); plan written after Phase 1 lands |
| 3 | **Journal layer v1** | Platform-level observability: P&L calendar, equity curves + drawdown, MAE/MFE capture, discipline metrics, tags/mistakes with provenance, notebook, entry/exit theses wired into both modules | Phase 1 merged (interleaves with #2 — both build on Phase 1's tables) | [Design](plans/2026-07-11-journal-layer-design.md); plan needs a fact-extraction pass over shipped Phase-1 code |
| 4 | **GEX lab Phase 1.5 — Half B** | Reflection family registration + deterministic verdicts; `edge/gex.md` becomes evidence-bearing | **~20 closed lab trades** (data-gated, not engineering-gated — calendar set by how often A+ setups occur) | Scoped in the design doc §Phase 1.5 |
| 5 | **Journal layer v2** | Session-Review coach (Meridian COACH contract), auto trade tagger (rules first, LLM second), weaknesses profile artifact, per-trade charts, Edge Score radar | Journal v1 shipped + a deliberate LLM-spend decision (default-off like deep-analysis) | [Design](plans/2026-07-11-journal-layer-design.md) |
| 6 | **GEX lab Phase 2** | Live intraday engine: session-resident local process, TTL bar path, own SSE channel, in-cockpit alerts, optional premium tracking / paid data | Lab evidence: the morning-static workflow is genuinely used AND setups show promise (Half-B verdicts better than "hunch") | Sketch in the design doc; needs its own design pass (data-feed decision) first |
| 7 | **Platform extraction** | Promote execution layer (locks, caps, adapters) out of the swing module; possible shared provider client | A second module **earns execution** (per North Star: evidence first) | Policy in [ARCHITECTURE.md](ARCHITECTURE.md) — deliberately unplanned until triggered |
| 8 | **The rename** | Repo/package `swing-screener` → `meridian` | Platform namespaces exist + a third module makes the suite shape undeniable | Policy in ARCHITECTURE.md |

## Standing coordination notes

- **Cockpit Phase-3 plan** (four screens, dashboard restructure) exists on its own unmerged
  branch (`docs/plans/2026-07-11-desktop-ui-phase3.md`, commit 85eab95). It touches the same
  `App.tsx` as GEX Phase 1's Task 18 — whichever executes second rebases.
- **Swing-module experiment backlog** (regime breakdown, volume dry-up, pocket pivot,
  retest-limit entry, RS-vs-SPY…) runs on its own track through the existing weekly
  propose/settle machinery — it is module-1 tuning, not a suite phase, and doesn't block
  anything above.
- Nothing on this roadmap touches the seven production Azure jobs until #6 at the earliest;
  phases 1–5 are local + cockpit + docs by design.

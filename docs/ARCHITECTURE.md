# The Trading Suite — Architecture & Module Contract

**Status:** adopted 2026-07-11. This document declares the direction; the filesystem follows
incrementally (see "Extraction policy"). The repo/package keeps the `swing_screener` name
until a rename earns its cost.

## What this system is

A personal trading **suite**: independent strategy **modules** built on a shared **platform**
of data access, persistence, evidence/learning machinery, a desktop cockpit, and (future)
execution infrastructure. The suite exists to find and validate trading edges with honest
statistics, then — only after evidence — route capital to them.

Current modules:

| # | Module | Charter | Horizon | Book(s) |
|---|--------|---------|---------|---------|
| 1 | Swing screener (HA continuation + reversal) | [NORTH_STAR.md](NORTH_STAR.md) | days–weeks | `research` / `live` |
| 2 | GEX options lab | [OPTIONS_LAB.md](OPTIONS_LAB.md) | intraday | `options-lab` / `robinhood` |

Module charters are sovereign within their scope: module 1's "not day-trading" non-goal
does not bind module 2, and vice versa. This document governs only what is *shared*.

## Platform layers (what every module gets)

1. **Data** — provider fetch seams with retry/backoff/jitter, per-module cadence contracts
   (`data/fetch.py` completed-bars-only day cache for batch modules; snapshot seams like the
   options chain client for on-demand). Rule: a module never bends another module's cache
   invariants — it adds its own seam.
2. **Persistence** — one SQLAlchemy Base, one engine factory (SQLite local / Azure SQL prod),
   one Alembic chain. Modules own their tables outright; cross-module writes are forbidden.
   Book/account fencing (`account`, `strategy` columns) keeps every aggregate module-pure.
3. **Evidence & learning** — the honesty machinery: unit-agnostic R-multiple stats core with
   clustered bootstrap CIs (`analytics/performance.py`), pre-registered experiment registry +
   settlement state machine, reflection/verdict grader, per-module `edge/<module>.md`
   playbooks. Each module declares its own **cluster key** (swing: ticker; GEX lab: session)
   and its own reflection family. Numbers cross module boundaries only as clearly-labeled
   comparisons, never pooled.
4. **Cockpit** — the desktop shell (pywebview + FastAPI factory + React). Each module ships a
   router factory (mounted before the static catch-all) and a view registered in the module
   nav. Shared contracts: Stat-dict for statistics, plain values for deterministic facts,
   503-with-friendly-detail, no URL/credential leaks, SSE watermarks for every table a view
   renders.
5. **Ops** — env-first `Settings`, the Azure container-job chassis (one image, eastern-gate
   entrypoint, heartbeats, email transport). Modules opt in per job; a module with no cloud
   footprint (GEX lab Phase 1) simply doesn't register one.
6. **Execution (future platform layer)** — today the broker adapters, execution locks, caps,
   and disarm ceremony live inside module 1. They are *designed* to be promoted to the
   platform when a second module earns execution; the money-safety spine (off-by-default,
   independent locks, mandatory caps, human gate) is a suite-level invariant already.

## The module contract (what a module must bring)

A new module ships, at minimum:

1. **A charter** — `docs/<MODULE>.md`: purpose, scope, principles, non-goals. Its learning
   prompts reason from its own charter.
2. **A frozen config dataclass** in its own package — never new fields on another module's
   config (optimizer/variant sweeps are scoped per config).
3. **Its own tables** on the shared Base + Alembic chain, with `account`/book fencing and
   lifecycle granularity matching its horizon (Date for daily books, DateTime intraday).
4. **An edge file** (`edge/<module>.md`) and a declared cluster key for all of its statistics.
5. **A cockpit presence** — router factory + registered view — and/or a CLI
   (`python -m swing_screener.<module>.run`).
6. **Test discipline** — pure engines, no-network seams, tests mirroring the package.
7. **Zero writes** to any other module's tables, config, or edge files.

## Extraction policy (strangler fig, not big bang)

The suite is live in production (seven scheduled Azure jobs). Physical restructuring follows
proven need, not aesthetics:

- Shared code is **promoted to a platform namespace when it gains its second consumer** —
  e.g., broker/execution moves out of module 1 when a second module earns execution;
  a shared provider client appears when a module needs non-yfinance data.
- Wrong-home code is moved **when touched for another reason**, not in dedicated churn PRs.
- The package/repo rename (if ever) waits until the platform namespaces exist and a third
  module makes the suite shape undeniable. GitHub redirects renames; imports don't.
- Anything that risks the nightly production loop needs the same evidence bar as a strategy
  change: what breaks, how we know, how we roll back.

## Naming

Until a rename earns its cost: the repo and Python package stay `swing-screener` /
`swing_screener`; docs and the cockpit masthead may say "trading suite". The dissonance is
acknowledged and accepted — identity lives in the charters, not the import path.

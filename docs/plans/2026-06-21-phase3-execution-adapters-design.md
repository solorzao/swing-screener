# Phase 3 — execution adapters + the autonomy gate — design

**Date:** 2026-06-21
**Governed by:** [North Star](../NORTH_STAR.md) (the *Execution arc*, principles #1/#3/#4/#6, the kill switch) ·
continues the [learning-loop design](2026-06-20-learning-loop-design.md) · consumes the
[Phase-2 insight engine](2026-06-20-phase2-insight-engine-design.md) `OrderIntent`.
**Status:** design agreed (brainstorm 2026-06-21); not yet implemented.
**Builds on:** Phase 0 (honest stats, #41), Phase 1 (edge playbooks + reflection, #43), Phase 2 (the
insight engine: conviction-graded `OrderIntent`s + the calibration loop, #45) — all shipped + live.

## Purpose

Give the Phase-2 `OrderIntent` somewhere to *go*: a pluggable **execution adapter** that turns each
intent into a concrete, idempotent, logged order — first as a human-placeable **ticket** (no money),
then as a **paper** position that the existing fill engine tracks to a real outcome — so the full
`OrderIntent → execution → outcome` flow is proven *before a dollar is risked*. Alongside it, the
**safety scaffolding** the money boundary will need (a default-off master switch + kill switch, hard
risk limits, and an advisory **autonomy gate** that measures whether conviction is actually
calibrated) is built now, while it has nothing dangerous to guard — so the guardrails exist before
there is ever a temptation to move them. **Robinhood / live broker execution is Phase 4**; this phase
only shapes the seam for it.

## Decisions (brainstorm 2026-06-21)

1. **Scope = interface + `manual` ticket + `paper` + safety scaffolding.** Build the adapter
   interface, the human-placeable order ticket, the paper adapter that opens real (simulated)
   positions, *and* the kill switch / hard limits / autonomy-gate scaffolding. (Chosen over
   interface-only or manual-only — the user wants the safety architecture in place early.)
2. **`paper` reuses the fill engine via a first-class `account` dimension.** A `research | paper |
   live` discriminator on `PaperTrade`; the paper adapter opens an `account="paper"` row and the
   existing bar-by-bar stepper (`advance_open` / `evaluate_exit`) fills + closes it. **No new
   lifecycle code.** Every existing research aggregate pins to `account="research"`, so the curated
   intent book is isolated *by construction* — double-counting is unrepresentable, and `live` is free
   in Phase 4. (Chosen over a minimal `source` tag — which leaves the trap live in every read path —
   and over a separate table, which would duplicate the fill engine.)
3. **The adapter is an injected seam, batch-dispatched.** Passed into `send_digest` like the existing
   `smtp_send` / `deep_analyze_fn` seams, and dispatched once per run in a single try/except boundary
   (mirroring `score_analyst_calls`). Tests inject a fake — **the suite can never place an order.**
4. **Master switch = `execution_mode ∈ {off, paper, live}`**, default `off`, re-read from env every
   run; the **kill switch** is just setting it back to `off`. `off` = exactly today's render-only
   behavior. `paper` emits the ticket *and* opens a paper position. `live` = Phase 4. "manual" is not
   a separate mode — it is the structured ticket that `paper`/`live` produce. (Chosen over two
   booleans, which admit a meaningless 'disabled + not-dry-run' state.)
5. **Hard limits clamped in code, sourced by summing the `ExecutionLog`.** Per-day notional cap,
   per-day realized-loss cap, and max-concurrent-positions cap — in addition to the existing
   per-trade risk + `max_shares`. Enforced *inside* `submit()` (never trusting the caller, the
   `_clamp_conviction` template); over a limit → `OrderResult(status="skipped", reason)`, logged, no
   position. Summing the log is always consistent (no decremented-counter reset bug).
6. **Autonomy gate: built now, advisory-only, human-gated.** Mirrors `propose()`. Reads the
   `forward_confirmed` verdicts sidecar (proven edge) + a **real calibration test** — `high`
   out-earns `low` via a clustered CI / two-sample test *with teeth* (upgrading `analyst_calibration`
   from bare means). Emits a reviewable report; returns `"insufficient data"` until thresholds are
   met; **never flips `execution_mode`.** (Calibration bar starts at `high > low`, tightenable later —
   chosen over demanding the full `avoid<low<medium<high` ladder, which may never cleanly pass.)
7. **Robinhood is Phase 4.** The interface is shaped to map onto its `review → place` MCP pattern, but
   this phase ships **zero broker code** and needs no broker secrets (paper uses our own engine).

## Components

- **`ExecutionAdapter` (Protocol)** — `submit(intent, *, session, run_date, idempotency_key) ->
  OrderResult`, with `name`. `OrderResult{status, account, detail, trade_id, broker_order_id}`
  (`status ∈ recorded | filled_paper | skipped | rejected`). New module `pipeline/execution.py`.
- **`manual` adapter** — builds the structured order ticket (side / limit / shares / stop / target)
  from the intent, writes one `ExecutionLog` row, opens no position. `status="recorded"`.
- **`paper` adapter** — runs the limit checks, then composes the manual ticket *and* opens a
  `PaperTrade(account="paper")` from the intent's fixed levels + sized shares. `status="filled_paper"`
  (or `skipped` if a limit blocks). The existing stepper closes it on later runs.
- **`OrderResult` / order ticket** — the deterministic order spec: `side="long"`, `limit_price =
  entry_ceiling` (buy at-or-below the deterministic ceiling), `order_type`/`time_in_force` defaults,
  `shares`, `stop`, `target`. Rendered in the digest; logged. Levels are read, never set.
- **`account` column on `PaperTrade`** (`research | paper | live`, default `research`) + a migration
  backfilling existing rows to `research`. Existing reads (`load_closed_paper_trades`,
  `score_analyst_calls`, leaderboards, `analyst_calibration`) gain an `account="research"` filter.
- **`ExecutionLog` (new table)** — one row per `submit`: pick keys, run_date, account, mode, the order
  spec, `idempotency_key` (UNIQUE), status, detail. Serves idempotency + audit + the hard-limit sums +
  the manual record. A migration (single head).
- **Settings + `resolve_execution`** — `execution_mode` (env `SWING_EXECUTION_MODE`, default off) +
  the new caps (`SWING_MAX_DAILY_NOTIONAL`, `SWING_MAX_DAILY_LOSS`, `SWING_MAX_CONCURRENT`); a pure
  resolver returning the mode + a `Limits` object. Mirrors `resolve_risk_unit` + the `deep_analysis_*`
  default-off pattern.
- **`autonomy_gate` (advisory)** — `pipeline/autonomy.py`: reads the verdicts sidecar + scored
  `account="research"` `AnalystCall`s, runs the calibration test, returns a structured verdict
  (`ready: bool`, the stats, `"insufficient data"` reasons). A CLI + (optionally) a surfaced line in
  the reflection / dashboard. Never mutates config.

## Data flow

1. `send_digest` resolves `(mode, limits, adapter)` once (default `off` → a NoOp adapter; today's
   behavior). For each deep pick, after `build_order_intent`, the intent is collected.
2. After the picks are built, the run **batch-dispatches** collected intents through the adapter in one
   try/except: each `submit` checks idempotency (the `ExecutionLog` UNIQUE key) → checks the hard
   limits (summing the day's log + open positions) → records the ticket → (paper) opens the
   `account="paper"` `PaperTrade`. Results are rendered in the digest.
3. On later runs the existing stepper advances + closes the open `account="paper"` trades exactly as
   it does research trades; `score_analyst_calls` (already keyed to research) is unaffected.
4. Independently (CLI / reflection cadence), `autonomy_gate` reads proven edges + scored calls →
   emits its advisory report. It is read-only; promotion to `live` stays a human act in Phase 4.

## Boundary, error handling, testing

- **Boundary (North Star #1/#4):** money never auto-moves — `off` is the default and `live` is
  unbuilt; levels are copied verbatim from the rules engine; the gate only advises. The paper book is
  isolated from the research grid by the `account` dimension.
- **Error handling:** the whole execution dispatch is one try/except — any adapter failure logs and
  **never blocks the digest** (mirrors the PDF/deep-analysis seams). A blocked limit or a duplicate
  idempotency key is a normal `OrderResult`, not an error. Missing caps → treated as 0/unset → the
  conservative path (no notional cap means the per-trade risk + `max_shares` still bound it).
- **Testing:** the adapter is an **injected fake** in `send_digest` tests (no order can be placed).
  Pure tests: the limit sums (per-day notional/loss, max-concurrent, each at/over the edge),
  idempotency (a re-run/force-resend does not double-submit), mode routing (`off` = no side effect),
  `resolve_execution`. Integration: a `paper` intent opens an `account="paper"` row the existing
  stepper then closes; the research aggregates exclude it. The calibration test has **teeth** — a
  placebo / label-shuffle case must *not* pass, and `high>low` only fires with a real, clustered,
  CI-backed gap. Migration single-head; existing-row backfill verified.

## Open questions (resolve in the plan)

- **Should the analyst's calibration be scored against the research-grid baseline trade (today's
  `score_analyst_calls`) or the *paper* intent trade once paper exists?** Default: keep scoring
  against `research` for now (stable, already accruing); revisit when the paper book has volume.
- Exact limit defaults + units (notional in $, loss in $ or R, concurrent count); whether the
  per-day window is calendar-day or trading-day.
- The autonomy gate's sample thresholds (n per conviction bucket, distinct-ticker floor) — reuse the
  grader's `n>=20` / `>=8` clusters, or set its own.
- Where the gate surfaces (CLI only, vs a line in the weekly reflection, vs a dashboard panel).
- The order ticket's rendering in the digest/PDF (a new "Order ticket" block vs extending the
  existing order-intent block).

## Out of scope (later phases)

Live broker execution — the **Robinhood agentic / MCP adapter** (official first-party MCP exists as of
2026-05-27 but is beta, equities/long-only, **no paper sandbox**, and **desktop-per-session auth** that
likely conflicts with our headless cron — the key Phase-4 unknown to resolve first), OAuth token
storage (needs writable secret storage; KV is read-only via UAMI today), per-day intra-run halting via
a DB/file flag, and the analyst proposing its own variants / commissioned tests (Phase 4+). Autonomy is
switched on **last**, by a human, behind the gate + hard limits + the kill switch.

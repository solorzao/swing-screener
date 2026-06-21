# Phase 4 — the live broker arc (Alpaca paper now, armable-when-ready) — design

**Date:** 2026-06-21
**Governed by:** [North Star](../NORTH_STAR.md) (the Execution arc, #1/#3/#4/#6, "autonomy earned LAST, by a
human, behind the gate + hard limits + kill switch") · consumes the [Phase-3 execution adapters](2026-06-21-phase3-execution-adapters-design.md).
**Status:** design agreed (brainstorm 2026-06-21); not yet implemented.
**Builds on:** Phase 0/1/2/3 (all shipped + live). The Phase-3 seam was deliberately shaped for this.

## Purpose

Stand up the **live broker path** — submit → fill → **reconcile** → exit against a *real broker API* — and
prove the entire order lifecycle end-to-end with **zero real money and no interactive-auth blocker**, using
**Alpaca's paper sandbox**. Build the real-money safety layer *now*, while it has nothing to guard, so that
flipping to real money — months from now, when the autonomy gate can actually pass — is a **guarded config
change, not a code change**. Robinhood remains the eventual real-money target as a *later* adapter onto the
same seam, once its headless-auth gap is solved (a separate spike).

## The honest situation (why this is not "turn on autonomous live trading")

- **The autonomy gate cannot pass for months.** `conviction_calibrated` requires `n_high>=20 AND n_low>=20`
  scored calls across `>=8` distinct tickers + a clustered CI lower bound > 0. `AnalystCall` data only began
  accruing at the Phase-2 merge (#45) and is scored only after a shadow trade *closes*; the `low` bucket is the
  binding constraint. Lowering the floors would betray North Star #1/#2.
- **The gate never auto-arms** (it is SELECT-only). A human flips `execution_mode` by hand. So Phase 4 *shapes
  the live path and de-risks the lifecycle* — it does not flip the switch.

## Decisions (brainstorm 2026-06-21)

1. **Broker-agnostic, Alpaca paper first; Robinhood a later adapter.** The goal is autonomous live execution on
   whatever broker fits a headless cron. Alpaca has a free, resettable **paper sandbox** with **static API-key
   auth** (no OAuth, fits the Azure cron) and the **same API for paper→live** (swap host+key). Robinhood's
   agentic MCP has **no sandbox** (real money day one) and **desktop-per-session auth** that fights the cron, so
   it is unsuitable to *validate the lifecycle* — it becomes a later sibling adapter.
2. **Scope = the full live path, armable-when-ready.** Build the broker client + Alpaca-paper backend + the live
   adapter + `reconcile_live` + the ExecutionLog broker columns + the real-money safety hardening + the
   gate-gated arming guard — all validated on Alpaca paper, all real-money paths built + tested but **inert**
   during the paper phase. (Matches the Phase-3 "scaffold safety early" choice.)
3. **The broker is an injected seam.** A `BrokerClient` Protocol injected like `smtp_send`/`anthropic_client`;
   tests inject a `FakeBroker` and **can never hit a real API**.
4. **`account="live"` = broker-reconciled, never simulated.** Which broker + endpoint (alpaca-paper /
   alpaca-real / robinhood) is **config recorded on the `ExecutionLog`**, not a new account value.
5. **The broker owns the fills.** The live adapter does NOT open a position at submit (fill price unknown);
   `reconcile_live` materializes it from broker truth. The bar-stepper **must never touch a `live` row**.
6. **Real money needs three independent locks** (below); Alpaca paper needs none of them.

## Components

- **`BrokerClient` (Protocol)** — `submit_order(spec) -> BrokerOrder`, `get_order(broker_order_id)`,
  `get_positions()`, `cancel_order(id)`, `cancel_all_orders()`, `get_account()`. New module `pipeline/broker.py`.
  A `FakeBroker` test double (scriptable fills/positions) + an `AlpacaBroker` (REST, paper endpoint via
  `SWING_ALPACA_KEY`/`SWING_ALPACA_SECRET`/`SWING_ALPACA_HOST`, through the existing `get_secret` seam).
- **`LiveAdapter`** (`name="live"`, `LIVE_ACCOUNT="live"`) in `pipeline/execution.py` — the 4th adapter. `submit`:
  the per-submit kill-switch re-check → the real-money lock check → `_limit_block` → `broker.submit_order(...,
  client_order_id=idempotency_key)` → `add_execution_log(status="submitted_live", broker=..., broker_order_id=...,
  broker_status=...)`. Opens **no** `PaperTrade`. On a broker reject → `rejected_live`.
- **ExecutionLog broker columns** (+ migration): `broker: String(16)`, `broker_order_id: String(64) | None`,
  `broker_status: String(32) | None`. New statuses `submitted_live` / `filled_live` / `canceled` /
  `rejected_live`; `submitted_live` joins `_LIMIT_COUNTING_STATUSES` (a working order reserves its notional).
- **`reconcile_live(session, broker)`** in `pipeline/reconcile.py` — the polling pass, sibling to `advance_open`,
  run each scheduled cycle. Pulls broker order/position state; on a fill materializes the `account="live"`
  `PaperTrade` (`entry_price`/`risk` from the **broker** fill) + flips the ExecutionLog to `filled_live`; on a
  broker-side close writes exit fields + `realized_r` (broker prices) + an `ExitEvent` (`is_paper=False`,
  `account="live"`). **Idempotent on `broker_order_id`** (materialize each fill once).
- **Stepper exclusion** — `advance_open` / `load_open_paper_trades` (for stepping) gain `account != "live"`, so
  the simulator steps `research`+`paper` only and `reconcile_live` exclusively owns `live`. Two disjoint engines.
- **The arming guard** — a pure `can_arm_real_money(settings, *, gate_ready) -> (bool, reason)`: real money
  requires `execution_mode=="live"` AND `SWING_BROKER_ALLOW_REAL_MONEY=="yes"` AND `gate_ready`. Checked in the
  live adapter before any **real-money** submit; an Alpaca-**paper** endpoint bypasses it (fake money).
- **Real-money limits** — when the endpoint is real money, the adapter **refuses to submit** unless the per-day
  notional / loss / max-concurrent caps are all set (no `None`/unbounded), and a **$ daily-loss circuit
  breaker** is added alongside the existing R one (real equity now exists).
- **Kill switch for live** — a **per-submit** re-read of `execution_mode` / a DB halt flag (not once-per-run);
  flipping to `off` halts the *next order* and fires `broker.cancel_all_orders()` to pull resting orders.

## Data flow (Alpaca paper, this phase)

1. Digest builds an `OrderIntent` (Phase 2) → batch-dispatch (Phase 3) → `LiveAdapter.submit` → per-submit
   kill-switch check → `_limit_block` → `broker.submit_order(client_order_id=idempotency_key)` →
   `ExecutionLog(status="submitted_live", broker_order_id=...)`. No position yet.
2. Each scheduled cycle, `reconcile_live` polls the broker: a fill → materialize the `account="live"` PaperTrade
   from the broker's price; a venue close → exit fields + `realized_r` + `ExitEvent`. Idempotent.
3. The bar-stepper (`advance_open`) skips `account="live"` entirely; the dashboard's exit/account facets already
   surface `live` (Phase-3 account dimension + the exit-events account facet).

## Boundary, error handling, testing

- **Boundary (North Star #1/#4):** money never auto-moves — default `off`; real money needs the three explicit
  locks AND the gate; levels stay deterministic (broker orders are placed at the rules-engine `limit_price`/stop/
  target, never recomputed). The advisory gate still only advises.
- **Error handling:** the broker seam degrades like the others — a broker/API failure logs and never blocks the
  digest; a partial fill / pending order is a normal reconcile state, re-polled next cycle; reconcile is
  idempotent so a double-poll never double-books; a missing/garbled broker response is caught, not fatal.
- **Testing:** `FakeBroker` (no network) drives the full submit→fill→reconcile→exit lifecycle (incl. partial
  fills, rejects, venue closes); the **real-money guard refuses** without all three locks; the **kill switch**
  halts the next submit + cancels open orders; the **stepper never touches a `live` row** (an integration test
  that `advance_open` leaves live rows untouched while `reconcile_live` owns them); limits enforced; migration
  single-head + backfill. The Alpaca REST client is unit-tested against recorded/fake responses — **no live
  network in CI**.

## Open questions (resolve in the plan)

- The exact Alpaca order mapping (our `limit_price` + stop/target → an Alpaca bracket order vs separate
  stop/limit children; whether Phase-4 paper uses a simple limit entry + a separate reconcile-managed exit, or a
  native bracket). Lean: **start with a limit entry; manage the exit via reconcile** (keeps the broker contract
  minimal), revisit brackets later.
- The reconcile cadence (every screen run, every digest run, or a dedicated job) and how it sources "current
  positions" for the exit decision.
- Whether `realized_r` for a live trade uses the broker's fill prices end-to-end (yes) and how partials map to
  the existing `partial_r`/`remaining_frac` fields.
- Live limit **default values** + the `$` circuit-breaker threshold (set conservatively; confirm units).
- Where the per-submit halt flag lives (env re-read vs a DB `system_flags` row) — env re-read is simplest;
  a DB flag enables halting without a redeploy.

## Out of scope (Phase 5+)

The **Robinhood live adapter** (a later sibling onto the `BrokerClient` Protocol) + its **OAuth writable-token
storage** (the UAMI's Key Vault is read-only; the lowest-friction fix is an `oauth_tokens` SQL table since the
UAMI already has SQL write — but Alpaca's static keys don't rotate, so this is deferred with Robinhood) + the
**headless/service-auth spike** for Robinhood; auto-arming (always a human act); fractional/short/options orders.

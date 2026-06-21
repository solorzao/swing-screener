# Phase 5 — the Robinhood approval surface + Alpaca arming readiness — design

**Date:** 2026-06-21
**Governed by:** [North Star](../NORTH_STAR.md) (the Execution arc, #1/#3/#6, "autonomy earned LAST, by a
human") · consumes the [Phase-4 live broker](2026-06-21-phase4-live-broker-design.md) seam.
**Status:** design agreed (brainstorm 2026-06-21); not yet implemented.
**Builds on:** Phase 0-4 (all shipped + live). Preceded by a sourced **Robinhood headless-auth feasibility
spike** (2026-06-21).

## The spike that shaped this phase

A multi-angle, adversarially-verified spike asked: *can Robinhood authenticate headlessly so the Azure
cron trades real money unattended?* **Verdict: NO-GO.** (1) The official agentic MCP
(`agent.robinhood.com/mcp/trading`) is OAuth 2.1 + PKCE, a **public client with no device-code or API-key
path** — Robinhood's own docs say you "can only authenticate your agent on a **desktop device**." (2) A
`refresh_token` grant exists, but Robinhood **publishes nothing** about its lifetime/rotation/re-MFA; the
token appears to rotate per call (hostile to a fire-and-forget cron), and there is **no primary source or
reproducible cloud-cron deployment** proving it survives weeks unattended. (3) The unofficial `robin_stocks`
path is **infeasible headless** (a post-2024 "sherrif" challenge needs a mobile-app tap). (4) ToS **§29.1**
requires express written consent for API use and **§37.8** permits deactivation at sole discretion — a
datacenter IP doing scripted re-auth risks a **funds-frozen** lockout. By contrast **Alpaca** (static key,
no OAuth, no 2FA, paper→live = host+key swap) is genuinely cron-native — the only high-confidence headless
real-money path, and the seam + paper backend already support it.

## Decisions (brainstorm 2026-06-21)

1. **Headless Robinhood = NO-GO.** No headless Robinhood live adapter. Robinhood's official MCP is used only
   in its sanctioned, **human-in-the-loop** shape.
2. **Alpaca is the autonomous real-money broker; Robinhood is a human-approval surface (A+B).** Honors both
   the North Star (a systemic, unattended edge) and the user's Robinhood preference (kept in the loop).
3. **Focus = the Robinhood approval surface NOW + Alpaca arming readiness.** The approval surface is usable
   immediately (act on picks by hand, no gate); the Alpaca real-money flip is mostly built but still gated by
   the autonomy gate (months out), so Phase 5 makes it *armable-when-ready* (runbook + preflight + the gate
   countdown) rather than flipping it.
4. **Nothing in this phase arms execution or moves money.** Default stays `off`; real money still needs all
   three locks (`execution_mode=live` AND `SWING_BROKER_ALLOW_REAL_MONEY=yes` AND `autonomy_gate.ready`);
   Robinhood holds **no stored credentials and makes no API call**.

## Workstream A — the Robinhood human-approval surface

- **A `manual` execution mode.** `execution_mode` gains a fourth value `manual` (default stays `off`,
  validated set `{off, manual, paper, live}`); `_adapter_for_mode("manual")` returns the existing Phase-3
  `ManualAdapter`, which records the order **ticket** to the `ExecutionLog` (audit + idempotency) and
  **opens no position, moves no money, contacts no broker**. The per-submit/limit machinery is unchanged.
- **A "Proposed orders" rendering.** A dedicated digest section (+ a structured artifact) lists each pick's
  exact, Robinhood-placeable instruction — *"Buy AMD, limit ≤ $101, 40 shares; then set stop $95, target
  $110 — HIGH conviction"* — derived from the recorded tickets / `OrderIntent`s. You read it and place it by
  hand on the Robinhood app (or hand it to Robinhood's official `review → place` MCP at your desk).
- **Boundary:** no Robinhood API call, no stored Robinhood credentials, no auto-placement. Rides entirely on
  the existing `OrderIntent` + `ManualAdapter`, so it's money-safe and usable the day it ships.

## Workstream B — Alpaca arming readiness

- **A preflight readiness CLI** (`python -m swing_screener.pipeline.preflight`) — READ-ONLY. Runs the go-live
  checklist and prints a blunt GO/NO-GO: broker reachable (`get_account` succeeds), account funded, **all**
  hard caps set (`real_money_limits_ok`), `is_real_money()` correct for the configured host, and the autonomy
  gate's status (via `autonomy_gate`). It **never arms** anything — it reports whether arming *would* be safe.
- **The autonomy-gate countdown.** The gate already computes `n_high`/`n_low`/cluster counts against the
  floors (20 + 20 scored calls, ≥8 distinct tickers). Surface them as *progress* — "continuation: 7/20 high,
  3/20 low, 5/8 tickers" — in `render_report` AND a one-line digest status, so the gate can be *watched*
  approaching rather than guessed. (Extend `CalibrationVerdict`/`render_report`; the floors are already
  constants.)
- **An arming runbook** (`docs/`) — the ordered ceremony to flip to Alpaca live: set the live host + all caps
  + `allow_real_money`, confirm the gate is `ready`, fund the account, run preflight, do a kill-switch drill,
  then flip `execution_mode=live`. Plus a small audit that the live caps are genuinely enforced and
  `is_real_money()` flags the Alpaca live host.
- **Boundary:** changes nothing about the default; nothing here can arm execution.

## Data flow

1. **Approval (A):** digest builds `OrderIntent`s (Phase 2) → with `execution_mode=manual`, the dispatch loop
   runs the `ManualAdapter` → tickets recorded → the "Proposed orders" section renders them → the human places
   them on Robinhood. No broker, no positions.
2. **Readiness (B):** the human runs `preflight` (and reads the gate countdown in the digest) to see whether
   the Alpaca-live flip is safe and how close the gate is. The flip itself stays a deliberate human act in a
   later step, once the gate is `ready`.

## Boundary, error handling, testing

- **Boundary (North Star #1/#3):** money never auto-moves; `off` is the default; real money needs all three
  locks; preflight + the countdown are read-only; Robinhood holds no creds and makes no call.
- **Error handling:** `manual` mode degrades like the other adapters (a ticket failure logs, never blocks the
  digest); preflight catches a broker/network error and reports it as a NO-GO line, never raising.
- **Testing:** `manual` mode selects the `ManualAdapter` + renders the proposal (no broker constructed — a
  test asserts no order/position); the proposal rendering is unit-tested off recorded tickets; preflight is
  tested against a `FakeBroker` (reachable/funded/unfunded) + `Limits` (caps set/unset) + a stubbed gate,
  asserting the GO/NO-GO lines and that it performs NO writes/arming; the countdown math is a pure test
  (n/floor progress per play type). No live network anywhere.

## Open questions (resolve in the plan)

- The "Proposed orders" artifact form (digest section only, vs also a structured JSON/CSV the desktop RH agent
  could consume) — lean: digest section + a simple structured file, defer a true RH-MCP review→place export.
- Whether `manual` mode should also surface in the dashboard, or digest-only for now.
- Whether to track placed/skipped on a proposal (an ExecutionLog status update) — lean: defer; the proposal is
  advisory and the human acts off it.
- Preflight's "account funded" threshold + whether it checks buying power vs the configured caps.

## Out of scope (Phase 6+)

A true Robinhood `review → place` MCP export / integration; placed-vs-skipped proposal tracking; the actual
real-money arming flip (a deliberate human act once the gate passes); the partial/fractional fill model; other
headless brokers (Tradier/IBKR). Auto-arming is never in scope — autonomy is always a human act.

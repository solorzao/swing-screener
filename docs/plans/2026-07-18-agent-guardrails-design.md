# Agent Guardrails — cockpit brake state for the live agent

**Date:** 2026-07-18
**Status:** **implemented (Tasks 1–20, plus Strategy Board Task 22)**; Strategy Board
**Tasks 23–24 (`GET /api/strategies` + the STRATEGY SCOPE panel) pending**. Originally
approved by Oliver (trip action, writable scope, mandatory set) and verified against the
codebase by a 4-lens seam review (enforcement, cockpit, data model, red-team).
Built on branch `claude/robinhood-agent-deploy-4df19b`, commits `3c0dac8`…`16435ee`.
Every **as-built amendment** noted inline below and in the
[implementation plan](2026-07-18-agent-guardrails-implementation.md) is binding where it
diverges from the original design text. Operator-facing truth lives in
[the arming runbook](../runbooks/arming-alpaca-live.md) (brake model, trip protocol,
mode-off caveat, Azure appendix, Stage-0 drill) and
[using-meridian.md](../using-meridian.md#the-safety-screen-7--the-guardrails-brake).
**Context:** the first-live-agent readiness audit (same date). The live Alpaca path is built
(three locks, caps, preflight, disarm, reconcile) but there is no one-click stop that reaches the
Azure jobs, no drawdown/trade-count breakers, and the cockpit deliberately writes no config.

## Decisions (made by Oliver)

1. **Trip action = hard DISARM.** Any breaker trip runs the proven sweep: pull entry-side (buy)
   orders, verify/restore protective stops, never flatten positions. Re-arm is a manual human act.
2. **Writable scope = breakers + HALT only.** Arming (`SWING_EXECUTION_MODE`,
   `SWING_BROKER_ALLOW_REAL_MONEY`) and sizing (risk $/trade, account equity) stay in the
   env/IaC ceremony. The cockpit gets one-click power to make the system safer, never more
   aggressive.
3. **Mandatory for real money:** max daily loss ($), max trades/day, **and max drawdown ($)**.
   Loss-streak halt is optional. An unset mandatory breaker refuses real-money orders
   (extends the existing three-caps mandate philosophy).

## The invariant: master arm vs. subordinate brake

`SWING_EXECUTION_MODE` (env, per-job, ceremony-controlled) stays the **master arm**. The
guardrails are a **subordinate brake**: dispatch submits only when `mode == live` AND the brake
is released. The cockpit can slam or release the brake; releasing when the master is off does
nothing — the cockpit still cannot arm a disarmed system.

The brake is deliberately **not** framed as config. The Safety screen's CONFIG panel and
`GET /api/config` stay read-only ("no config write ever originates in the cockpit",
`cockpit/routers/safety.py:283-295`). Guardrails are operational safety **state** — DB rows, like
`DisarmEvent`, trade closes, and audit ACKs, which the cockpit already writes. This is also the
only control in the system that propagates cockpit → running Azure job mid-dispatch (env cannot);
it must be documented and tested as the primary remote stop, not as a copy of the kill switch.

## Data model

### `agent_guardrails` — single-row mutable state (id = 1)

| column | type | notes |
|---|---|---|
| `state` | String(16) NOT NULL default `'ok'` | `'ok' \| 'halted' \| 'tripped'` — one enum column, **no boolean in any WHERE clause** (sqlite renders `IS 1`, SQL Server rejects — the Journal-v2 trap) |
| `max_daily_loss_usd` | Float NULL | mandatory-for-real-money |
| `max_trades_per_day` | Integer NULL | mandatory-for-real-money |
| `max_drawdown_usd` | Float NULL | mandatory-for-real-money |
| `loss_streak_halt` | Integer NULL | optional |
| `hwm_anchor_date` | Date NULL | drawdown window start; compared against `PaperTrade.exit_date` |
| `hwm_baseline_usd` | Float NOT NULL default 0 | realized-$ equity at anchor |
| `trip_id` | Integer NULL | FK-ish to the electing `agent_guardrail_events.id` |
| `trip_reason` | String(256) NULL | `'max_drawdown_usd: -61.40 <= -50.00'` |
| `sweep_state` | String(16) NULL | `'complete' \| 'partial' \| 'pending'` — non-null only while/after tripped |
| `updated_at` | DateTime NOT NULL | |

State transitions are **atomic conditional UPDATEs** (the reconcile status-transition idiom):

- **trip:** `UPDATE … SET state='tripped', trip_id=:e, trip_reason=:r, sweep_state='pending'
  WHERE state != 'tripped'` — rows-affected == 1 elects the single owner of sweep + email + event;
  a concurrent second evaluator (8am digest vs 4:15pm screen, weekly+monthly Friday overlap)
  updates zero rows and does nothing.
- **clear:** `UPDATE … SET state='ok', trip_id=NULL, … WHERE trip_id=:acknowledged AND
  state='tripped'` — a stale cockpit screen can never clear a newer trip.
- **HALT:** cockpit sets `state='halted'` (a trip may overwrite `halted` → `tripped`; both block
  identically; clear returns to `'ok'`).
- **limit edits:** SET only their own columns, never state columns.

Reads on the hot path use a **column-select** (the `realized_r_on` pattern), never a re-queried
ORM entity — the dispatch loop holds one long-lived Session and identity-map staleness would
otherwise hide a mid-dispatch cockpit HALT.

### `agent_guardrail_events` — append-only history + Auditor feed

`kind` (`'edit' | 'halt' | 'trip' | 'clear' | 'sweep'`), `breaker`, `reason`, `values_json` Text,
`source` String(16) (`'cockpit' | 'digest' | 'screen'`), `created_at`. Mirrors
`DisarmEvent` + `SystemAudit.kind`. Every change appends one row; the cockpit renders history
from here.

### `PaperTrade.qty` — new nullable Integer column

The $-denominated breakers are **uncomputable today**: live trades store `realized_r` but no
share count, and the only join precedent (`_live_shares`, per-ticker newest-row heuristics)
mis-sizes on re-trades and partials. Fix at the source: `reconcile._materialize_fills` already
holds `order.filled_qty` and `log.shares` — stamp `qty` on the live `PaperTrade` at
materialization. Realized $ per trade = `(exit_price − entry_price) × qty`.
This same column unblocks the scoreboard's deferred live `realized_usd` (Stage-1 item) —
one fix, two consumers. Existing R-denominated env caps stay untouched underneath as a second net.

### Migration

One Alembic revision (`down_revision='c1d7f3e9a5b2'`): two new tables + the `PaperTrade.qty`
add-column, `server_default` on every NOT NULL column so the mssql ALTER succeeds on existing
rows. Docstring carries the standing notes: local sqlite gets tables via `create_all`;
create_all-born `local.db` needs `alembic stamp` before upgrade; verify `alembic heads` stays
single.

## Enforcement — two layers, both required

**Layer 1 — the block, inside `LiveAdapter.submit` (unconditional).** A new step between the
idempotency guard and the real-money guard: if `state != 'ok'` or a set breaker is breached,
refuse with a logged `status='skipped'` row, `detail='guardrail: …'` (clamp semantics: greppable,
non-counting, upgradeable). This placement is load-bearing three ways:

- It honors the module's stated rule that safety lives *inside* submit, never trusting the caller.
- It runs on the **Alpaca paper host too** (it sits outside the `is_real_money()` gate, like
  `_limit_block`) — so the Stage-0 drill rehearses every trip path with fake money. Hanging the
  brake off the real-money guard would make the drill a no-op.
- It does **not** sit in the broker layer: `disarm.py`'s stop-restore calls
  `broker.submit_order` directly, and protective-stop restoration must keep working *while
  tripped* — the trip action itself depends on it.

The **mandate** (unset mandatory breaker ⇒ refuse) is separate: a session-taking
`guardrails_mandate_ok(session)` in `db/repo.py`, invoked inside the `is_real_money()` block
right after `real_money_limits_ok`, refusing with `rejected_live` naming the missing breaker.
`settings.real_money_limits_ok` stays pure and untouched. Paper host stays exempt from the
mandate (matches existing philosophy), while set breakers enforce everywhere.

**Layer 2 — the response, in the dispatch loop.** A session-taking sibling of
`_execution_halted` beside the kill-switch block: on trip/HALT, stop the batch and run the trip
protocol below (broker + session are in scope there). Weekly/monthly digests share this loop;
exit-kind and on-demand jobs never dispatch.

**Evaluation points for state-driven breakers** (daily loss, drawdown, loss-streak — they change
on *closes*, not submits):

1. **Dispatch time,** after a lightweight `reconcile_live` pass (idempotent by design; broker
   already in scope when `mode==live`) — so a same-morning venue stop-out is counted *before*
   the morning's orders go out, closing the realized-only freshness hole.
2. **Evening screen,** immediately after the existing `reconcile_live` call — same-day exits trip
   the same evening. Broker may be `None` there on a secrets gap: persist the trip anyway with
   `sweep_state='pending'`.

## Day semantics

"Daily" = the threaded **`run_date`**, exactly as `_limit_block` keys the existing caps
(`ExecutionLog.run_date`, `PaperTrade.exit_date`). No calendar/ET day key — the 8am digest
deliberately runs under the prior evening's screen date, and a third clock would disagree with
the caps every single morning. Cross-boundary agreement (4:15pm trip vs next-morning dispatch)
is carried by the **persisted tripped state**, not by re-deriving "today".

- **Trades/day** = count of counting-status (`submitted_live`/`filled_live`) `account='live'`
  ExecutionLog rows for `run_date` (`execution_logs_for_day`, as-is).
- **Daily loss $** = Σ `(exit_price − entry_price) × qty` over closed live trades with
  `exit_date == run_date`.
- **Drawdown $** = `hwm_baseline_usd` + running-max of cumulative realized live $ since
  `hwm_anchor_date`, minus current cumulative — ordered by `ExitEvent.id` (the only reliable
  close order; closes are UPDATEs, `exit_date` has no time). Trip when the gap ≥
  `max_drawdown_usd`.
- **Loss-streak** = consecutive `realized_r < 0` from the newest `ExitEvent` where
  `ExitEvent.account == "live"` (`==`, never `.is_()`), joined via `trade_id`, null rows skipped.

## Trip protocol (strict order)

1. **Persist first:** the conditional-UPDATE trip election + `agent_guardrail_events` row commit
   before anything touches the venue. The brake holds no matter what follows.
2. **Sweep:** `pull_entry_orders` + `ensure_stop_protection`, with `key_suffix` derived from the
   **trip id** (not the wall-clock second) so cross-process re-runs collapse to the same
   client_order_ids. Also write a `DisarmEvent(reason='guardrail:<breaker>')` via the existing
   best-effort pattern — the breach scanner then surfaces the sweep with zero new Auditor code.
3. **Record outcome** on the state row: `sweep_state='complete'` or `'partial'` (mirror
   `'cockpit-partial'`) plus the unprotected list in the event row.
4. **Email:** clone `_emit_pending_exit_alert` exactly — compose, SEND first via
   `resolve_sender` (ACS in prod), then `EmailLog(kind='guardrail', alert_key=<event id>)` with
   the IntegrityError dedup catch. The evening-screen job gets this minimal sender wired in (its
   env already carries ACS config); the send is try/except-isolated so mail failure can never
   abort a sweep, and a mid-dispatch send can never abort the digest.

**Partial-sweep ownership:** while `sweep_state='partial'` or `'pending'`, every subsequent job
run (screen + digest) re-runs the sweep — safe because `ensure_stop_protection` skips
already-protected symbols — and the Safety screen renders **TRIPPED — SWEEP PARTIAL** loudly
*(as-built string: **`TRIPPED — SWEEP RETRYING`**, covering both `partial` and `pending`, and
it REPLACES the reason in the headline rather than appending to it — the reason moves to the
line below)*,
with the existing hold-to-confirm `/api/disarm` as the manual retry. This is also the crash
recovery: a death between trip and sweep self-heals at the next evaluation.

**Accepted residual (bounded):** at most one in-flight submit per process can land after a HALT
commit; it is capped, bracket-protected, and logged. The Stage-0 drill must include a
mid-dispatch HALT flip asserting the ≤1-order leak bound.

## Clear / re-arm semantics

- Clearing is a cockpit act: **acknowledge the trip reason first** (`disabled={!acknowledged}` so
  the hold cannot begin until ticked — `HoldToConfirm`'s `armed` prop is an async wait-gate and
  would fire instantly on tick if misused), then a 900 ms hold (the venue-adjacent tier).
- **Clearing a drawdown trip does NOT reset the HWM anchor.** If equity is still below the line,
  the trip re-fires at the next evaluation — correct, because the drawdown is still real.
  Resetting the anchor (fresh budget, e.g. after a deposit) is a separate, explicit edit action
  on the panel that appends its own `'edit'` event. *(Default chosen by Claude — flag if you want
  clear-implies-reset.)*
- No cool-down or second factor beyond ack + hold in v1 (the master arm still exists above the
  brake). External cash flows (deposits/withdrawals) are handled only via explicit anchor reset.

## Cockpit surface

- **Panel:** own section in `SafetyScreen`'s left column between ARMING LOCKS and HARD CAPS,
  with an honesty caption: *"a brake, not a knob — env stays the master arm."* Never inside the
  read-only CONFIG panel.
- **API:** separate `GET /api/guardrails` (cheap, DB-only — the existing safety poll makes a
  real broker call per refresh and must not gain guardrail reads; *as-built amendment, Task 12:*
  the safety report and gate poll do carry read-only brake fields via the non-seeding
  `peek_guardrails` — display-grade, never seeding, never a venue call; `GET /api/guardrails`
  remains the panel's own richer surface) + `POST /api/guardrails`
  in the safety router beside `/api/disarm`, following its conventions: `_require_cockpit`
  header guard, single-flight lock → 409, snapshot invalidate + `action_nonce` bump in `finally`.
- **Error posture (critical):** the **primary state write propagates `SQLAlchemyError`** to the
  app-level 503 handler like the trade-close write. The `_record_disarm` swallow-everything
  pattern is reserved for the audit event row **only** — a missing G7 grant must never produce a
  200 that silently dropped the brake.
- **HALT control:** reuse the DISARM composition verbatim (900 ms hold, `onHoldStart` fires a
  `dry_run` sweep preview, `armed` = preview landed, alarm-panel posture).
- **In-process locking:** any cockpit endpoint that runs the sweep acquires the same
  `app.state.disarm_lock`; cross-process arbitration is the trip election + trip-id key_suffix.
- **Forms:** copy `CloseTradeForm`'s idiom (field/label/preview/error/actions,
  `ApiError.fieldErrors`, post-commit focus handoff) for the limits editor.
- **Glossary:** new terms (guardrail, HALT, trip, HWM/drawdown anchor, loss streak) in
  `glossary.data.json` with source-links; the citation-guard pytest enforces coverage.
- **UI verification (standing hazards):** CSS brace-balance count after any stylesheet change;
  iterate via `vite dev` + Playwright MCP screenshots (in-app browser screenshots hang);
  cockpit-ui has no test framework — the API layer carries the tests. Run `ruff` before pushing.

## Auditor integration

Teach `audit_compliance`/`audit_run` to classify guardrail activity (day granularity — intra-day
trip-vs-submit ordering is not provable from `created_date`):

- **Expected (info/warn):** trip followed by halt; `'guardrail:'`-detail skipped rows;
  `DisarmEvent(reason='guardrail:*')`.
- **Breach:** a counting live submit on a day the state says tripped; a trip with no matching
  `EmailLog(kind='guardrail')`; live submits while a mandatory breaker was unset;
  `sweep_state='partial'` persisting across a full day.

## Companion fix required (guardrail-counter integrity)

**Orphan adoption:** a crash between venue submit and the ExecutionLog write leaves a fillable
venue order that today resurfaces only as a `rejected_live` duplicate — invisible to the
reconciler and to every counter (trades/day undercounts; the orphan's exit never enters daily
loss/drawdown/streak). On a duplicate-`client_order_id` rejection, fetch the existing order
(`GET /v2/orders:by_client_order_id`) and upgrade the row to `submitted_live` with the real
`broker_order_id` (the `add_execution_log` upgrade path already supports non-counting →
counting in place).

## Surfacing (read-only additions)

- `GET /api/execution/safety`: a fourth mandate entry (guardrails) beside `caps_mandate`, so the
  locks panel stays honest.
- `preflight`: a guardrails check beside `_check_caps` (mandatory breakers set, state `'ok'`) —
  critical for real money, advisory for paper host.
- Masthead: the existing gate poll gains the brake state chip (`TRIPPED`/`HALTED` next to the
  execution-mode chip).

## Addendum (2026-07-18, approved): the Strategy Board

**Decisions (Oliver):** strategy selection is **tighten-only**, and the board ships on this
branch, reusing the guardrails plumbing.

**Visibility.** A cockpit STRATEGY SCOPE panel (Safety screen, below guardrails) lists every
play type with: an IN SCOPE / OUT lamp, playbook presence (`edge/<pt>.md` + verdicts), the
evidence tier (`hunch → replay_screened → forward_confirmed`), the ranking metric — the
**CI lower bound on expectancy** (the autonomy gate's own number; never the point estimate,
which invites picking noise) — plus n, forward shadow-book stats (`would_surface` facet), the
analyst-calibration countdown, and the gate-ready lamp. Rank order: tier first, CI floor
second. Served by a DB/file-only `GET /api/strategies` (no broker call).

**Selection model — two-level scope.**

- `SWING_EXECUTE_PLAY_TYPES` (env, Task 8) is the **ceiling**: the ceremony-controlled set the
  agent may ever trade. Changing the ceiling is an env/IaC act, always.
- `agent_guardrails.disabled_play_types` (new bounded-string column, comma-separated, default
  `""`) is the **cockpit subtraction**: one-click disable of any strategy (pure risk removal),
  and re-enable back up to — never past — the env ceiling (same logic as releasing HALT: the
  brake can return to what the master arm allows, it can never exceed it).
- Effective scope = ceiling − disabled. The dispatch filter and the submit-side brake both
  consult effective scope; Task 8 builds the filter behind an
  `effective_execution_scope(settings, session)` seam so the subtraction lands without rework.
- Scope changes append `agent_guardrail_events` rows (`kind='edit'`, breaker
  `'disabled_play_types'`) — Auditor-visible like every other brake write.

## Out of scope (v1)

- Unrealized P&L in breakers (venue-held stops bound open risk; realized-only is deterministic).
- Cockpit arming, mode flips, sizing writes (ceremony, by charter).
- Per-job guardrail divergence; cool-down timers; ET-calendar day keys.
- Robinhood anything (separate audit verdict: Alpaca is the venue).

## Testing

- **Unit:** transition guards (concurrent trip election, stale clear, edit-vs-trip clobber);
  breaker math on both sqlite and mssql fixtures (`== True`-style rendering trap); run_date
  keying vs the morning-digest prior-day date; mandate refusal shape; loss-streak ordering via
  `ExitEvent.id`; HWM with anchor persistence across clear.
- **Integration (FakeBroker):** trip mid-dispatch halts the batch, sweeps once, emails once,
  survives a partial sweep and re-runs it next cycle; qty stamping through
  `reconcile._materialize_fills`; orphan adoption.
- **Drill (Stage-0, Alpaca paper host):** flip HALT mid-dispatch (≤1-order leak bound); force a
  drawdown trip with a tight limit and watch the sweep + email + TRIPPED banner end-to-end.

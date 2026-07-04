# Arming runbook — flipping to Alpaca LIVE (real money)

This is the ordered, deliberate ceremony to move the screener from paper to **real money**
on Alpaca. It is the human act the whole Execution arc defers to last
([North Star](../NORTH_STAR.md) #1/#3/#6, "autonomy earned LAST, by a human").

> **This document arms nothing.** Reading it, running the CLIs it references, or running
> `preflight` moves **no money** and changes **no config** — they are read-only checks.
> Arming is the single manual act in step 7 (`SWING_EXECUTION_MODE=live`). Until then the
> default stays `off` and the screener never touches a venue.

Real money is fenced behind **three independent locks** plus **mandatory caps**, all
re-checked in code on every submit (`pipeline/execution.py::LiveAdapter.submit`) — never
trusting any caller-side gate. No single misconfiguration can move real money.

## Preconditions — the three locks and the caps

A real-money endpoint (`AlpacaBroker.is_real_money()` is `True` — see below) arms **only**
when **ALL THREE** of these hold (`settings.can_arm_real_money`); any one missing refuses,
naming the first failing lock:

| # | Lock | How it's set | Refusal reason if missing |
|---|---|---|---|
| 1 | `execution_mode == "live"` | `SWING_EXECUTION_MODE=live` | `execution_mode is not live` |
| 2 | `allow_real_money` | `SWING_BROKER_ALLOW_REAL_MONEY=yes` | `SWING_BROKER_ALLOW_REAL_MONEY is not set` |
| 3 | `autonomy_gate.ready` | earned, not set — the gate's verdict (see step 1) | `autonomy gate is not ready` |

Lock 2 accepts any of `1` / `true` / `yes` / `on` (case-insensitive); anything else (or
unset) leaves real money **disallowed**. Lock 1 is fail-safe: an unknown/garbage
`SWING_EXECUTION_MODE` coerces back to `off`, never to `live`.

On top of the locks, a real-money endpoint may **never run uncapped**
(`settings.real_money_limits_ok`): **all three** hard caps must be set or a real-money order
is refused (`rejected_live`, no broker order), naming the first unset cap:

| Cap env var | `Limits` field | Meaning |
|---|---|---|
| `SWING_MAX_DAILY_NOTIONAL` | `max_daily_notional` | max total $ notional booked per day (per account) |
| `SWING_MAX_DAILY_LOSS` | `max_daily_loss` | per-day realized-loss circuit breaker, as an **R threshold** (e.g. `2.0` blocks new orders once the account is at −2R for the day) |
| `SWING_MAX_CONCURRENT` | `max_concurrent` | max simultaneously-open live positions |

A **paper** broker (the Alpaca paper sandbox) needs none of this — it is not a real-money
endpoint, so the adapter skips the lock/caps guard entirely. The locks and caps gate **real
money only**.

## The ceremony (ordered)

Do these in order. Steps 1–6 are checks and a drill — none of them arms anything; only
step 7 flips the switch.

### 1. Confirm the autonomy gate is `ready` (lock 3)

The gate is **earned, not set**: it reports `ready` only once a play type has a
forward-confirmed edge AND calibrated conviction. Watch it approach with the countdown:

```bash
python -m swing_screener.pipeline.autonomy
```

Read the **Calibration progress** block. Each not-ready play type shows its distance to the
floors, e.g. `continuation: 7/20 high, 3/20 low, 5/8 tickers`. The gate is ready for a play
type when it reads `continuation: ready` — i.e. it has cleared **20/20 scored high + 20/20
scored low calls and ≥8 distinct tickers** (`MIN_LEADERBOARD_N = 20`, `_CLUSTER_FLOOR = 8`)
and its edge is forward-confirmed. The overall gate is `READY (advisory)` once **at least
one** play type clears it. Do not proceed past this step until the headline reads
`READY (advisory)`.

> The gate is **advisory** — it never flips `execution_mode`. It tells you the edge is
> proven and calibrated enough that the conversation *may* begin; you still decide.

### 2. Set the Alpaca **live** host + live credentials

Point the broker at the live trading host and supply the **live** (not paper) API key/secret.
The default host is the paper sandbox (`https://paper-api.alpaca.markets`); the live host is:

```bash
export SWING_BROKER=alpaca
export SWING_ALPACA_HOST=https://api.alpaca.markets
export SWING_ALPACA_KEY=<your LIVE Alpaca key id>
export SWING_ALPACA_SECRET=<your LIVE Alpaca secret>
```

`is_real_money()` is fail-safe: it reports `True` (real money) for **any** host that is not
the recognized paper host — the live host above, or any typo'd/unknown host. Only
`paper-api.alpaca.markets` is treated as fake money. (In Azure these are Key Vault secrets
injected as env vars; locally, your shell/secret store.)

### 3. Set ALL caps + the allow-real-money flag (locks 2 + the caps)

```bash
export SWING_MAX_DAILY_NOTIONAL=<max $ notional/day, e.g. 5000>
export SWING_MAX_DAILY_LOSS=<per-day loss breaker in R, e.g. 2.0>
export SWING_MAX_CONCURRENT=<max open live positions, e.g. 3>
export SWING_BROKER_ALLOW_REAL_MONEY=yes
```

All three caps are mandatory — omit any one and a real-money order is refused
(`rejected_live`). `SWING_BROKER_ALLOW_REAL_MONEY=yes` is the explicit, loud second lock;
keep it **unset** until you genuinely mean to arm.

### 4. Fund the Alpaca account

Fund (and ACH-clear) the live Alpaca account so it is `ACTIVE` with `buying_power > 0`.
Preflight's funding check is critical and will read `NO-GO` against an empty/unfunded account.

### 5. Run preflight → confirm **GO**

The read-only go/no-go check. It reads the broker, the settings snapshot, and the gate — and
**writes nothing, arms nothing**:

```bash
python -m swing_screener.pipeline.preflight
```

Confirm the verdict line reads **`GO`**. The critical checks (all must pass for GO) are
`config` (broker selected + mode is paper/live), `reachable` (`get_account` succeeds),
`funded` (`ACTIVE` + `buying_power > 0`), and `caps` (all three caps set). `is_real_money`
and `autonomy_gate` are **advisory** lines (a heads-up, never flipping GO) — but you should
see `is_real_money: live host -> REAL money` and `autonomy_gate: autonomy gate READY` here,
consistent with steps 1–2. A `NO-GO` names the failing critical check; fix it and re-run.

### 6. Kill-switch drill (rehearse the abort before you arm)

Before arming for real, rehearse the disarm. With a live adapter wired, the dispatch loop
**re-reads `SWING_EXECUTION_MODE` before every submit** (`notify/run.py::_execution_halted`):
if it is no longer `live`, the loop **halts** the rest of the batch AND pulls the
**entry-side (buy) resting orders** — never a blanket cancel, which would strip the
bracket's protective stop legs off open positions. Any position whose stop leg is found
dead gets a plain GTC stop re-submitted at the ExecutionLog ticket's recorded level
(copied, never computed); a position with no restorable level is reported `UNPROTECTED`.

Rehearse it: start a run with execution armed, then mid-run set `SWING_EXECUTION_MODE=off`
(the loop re-reads it fresh) and confirm in the logs that dispatch halted
(`execution kill switch: halting dispatch ... and pulling entry-side resting orders`), that
entry orders were cancelled, and that every open position still shows a live sell stop at
the venue. Satisfy yourself the abort works **before** any real order rests at the venue.

### 7. Flip `SWING_EXECUTION_MODE=live` — arm

This is the single act that arms. With locks 1–2, the caps, funding, and a `GO` preflight all
in place, and lock 3 (`autonomy_gate.ready`) earned:

```bash
export SWING_EXECUTION_MODE=live
```

The next dispatch run submits one live order per pick (idempotent per pick/run/side), records
the `broker_order_id` to the `ExecutionLog`, and opens **no** position at submit (the
reconciler materializes the position from the broker's eventual fill).

### 8. Monitor

- **The digest** — the autonomy-gate status line and the proposed/recorded orders.
- **The `ExecutionLog`** — one row per submit: `submitted_live` (working at the venue, with
  `broker_order_id`), `skipped` (a hard-limit clamp), or `rejected_live` (a lock/cap refusal
  or a broker error). This audit table is the single source of truth for every live order.
- **Exit alerts** — the intraday exit cadence + the reconciler closing live positions.

## Rollback / kill switch

To disarm at any time:

```bash
export SWING_EXECUTION_MODE=off
```

`execution_mode` is **re-read every run** (and re-read before every submit inside a run), so
setting it to `off` (or anything other than `live`):

1. stops the **next** order from being submitted, and
2. pulls the **entry-side (buy) resting orders** and verifies every open position still
   holds its protective sell stop, restoring a dead stop leg at the ExecutionLog ticket's
   recorded level (the per-submit kill switch in `notify/run.py`). The bracket stop and
   target legs of open positions are **left working** — they are the protection.

The standalone `python -m swing_screener.pipeline.disarm [--dry-run]` command performs the
same entry-cancel + stop-verification sweep on demand and reports any position it had to
leave `UNPROTECTED` (no live stop and no recorded level to copy).

Setting `off` is the safe, reversible disarm — it is exactly what the step-6 drill rehearses.
A working live order that has already **filled** is a position; reduce/close it through Alpaca
directly (the reconciler will then record the exit).

## What stays manual

- **Robinhood is a human-approval surface, never headless.** The Phase-5
  [feasibility spike](../plans/2026-06-21-phase5-approval-arming-design.md#the-spike-that-shaped-this-phase)
  found headless Robinhood auth a **NO-GO** (desktop-only OAuth, undocumented refresh-token
  lifetime, a datacenter-IP lockout risk). Robinhood holds **no stored credentials and makes
  no API call** here — you act on the "Proposed orders" list by hand. Alpaca is the only
  high-confidence headless real-money path.
- **Arming is always a human act.** Nothing in the screener flips `execution_mode` to `live`
  for you — not the gate, not preflight, not CD. Promotion to real money is a deliberate
  manual step, performed here, by you, once the gate is `ready`.

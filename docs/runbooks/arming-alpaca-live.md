# Arming runbook — flipping to Alpaca LIVE (real money)

This is the ordered, deliberate ceremony to move the screener from paper to **real money**
on Alpaca. It is the human act the whole Execution arc defers to last
([North Star](../NORTH_STAR.md) #1/#3/#6, "autonomy earned LAST, by a human").

> **This document arms nothing.** Reading it, running the CLIs it references, or running
> `preflight` moves **no money** and changes **no config** — they are read-only checks.
> Arming is the single manual act in step 9 (`SWING_EXECUTION_MODE=live`). Until then the
> default stays `off` and the screener never touches a venue.

Real money is fenced behind **three independent locks** plus **mandatory caps** plus the
**guardrails brake**, all re-checked in code on every submit
(`pipeline/execution.py::LiveAdapter.submit`) — never trusting any caller-side gate. No
single misconfiguration can move real money.

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

---

## GUARDRAILS — the brake (read this before the ceremony)

The three locks + caps are the **env** fence. On top of them sits a second, independent
fence you operate from the cockpit: the **guardrails brake**
([design](../plans/2026-07-18-agent-guardrails-design.md)). Understand it before you arm —
during the ceremony you have to *configure* it, and after you arm it is your one-click stop.

### The model: master arm vs. subordinate brake

- **`SWING_EXECUTION_MODE` is the master arm.** It lives in env (per job, per process) and
  changes only through the ceremony below (or, on Azure, the appendix). Nothing in the
  cockpit can set it.
- **The brake is the DB row `agent_guardrails` (single row).** The cockpit's Safety screen
  writes it. It can only **block**: dispatch submits when `mode == live` **AND** the brake
  is released. Releasing the brake on a disarmed system does nothing — *the cockpit can
  make the machine safer, never more aggressive.*

The brake has three states — `ok` (released), `halted` (you pressed HALT), `tripped` (a
breaker breached). Both non-`ok` states block identically.

### The breakers

| Breaker | Column | Mandatory for real money? | What it counts |
|---|---|---|---|
| max daily loss ($) | `max_daily_loss_usd` | **yes** | Σ `(exit − entry) × qty` over live trades closed on the run date |
| max trades/day | `max_trades_per_day` | **yes** | counting-status (`submitted_live`/`filled_live`) live `ExecutionLog` rows for the run date — a **runaway detector**, so set it **ABOVE** your expected daily intent count: breaching it *trips the brake*, and the trip sweep then cancels the day's own unfilled entries and needs a manual clear |
| max drawdown ($) | `max_drawdown_usd` | **yes** | gap from the high-water mark of cumulative realized live $ since the anchor date |
| loss streak | `loss_streak_halt` | optional | consecutive losing live closes, newest-first |

**Unset mandatory breaker ⇒ a real-money order is refused** (`rejected_live`, naming the
first missing one — `db/guardrails_repo.py::mandate_from_state`), exactly like an unset cap.
The paper host is exempt from the *mandate*, but a **set** breaker and an **engaged** brake
enforce everywhere, paper included — which is what makes the Stage-0 drill meaningful.

These are **dollars and counts** and they are **separate from the env caps above** (which
are R-denominated / notional). Both nets stay in place; neither replaces the other.

> **KNOWN LIMIT — the Batch-wait freshness window (merge of 2026-07-25).** With the
> deep-analysis **Batch** path enabled (`deepAnalysisBatch`, ON in prod since 2026-07-24),
> the digest's batch pre-pass runs *between* the dispatch-time live reconcile and the
> dispatch loop's breaker consult, and can wait up to ~60 minutes. A venue stop-out that
> fires **during** that wait is not reflected in the daily-loss/drawdown reads for that
> morning's dispatch — the exact realized-only freshness hole the dispatch-time reconcile
> exists to close. A trip recorded by another process IS still seen (the consult reads
> DB state fresh); only venue→DB realized P&L goes stale. The submit-side clamp and the
> mandate re-read remain the hard backstops. **Before arming with batching on**: either
> disable batching for the live trial, or land the pre-arming code item that moves (or
> repeats) the reconcile after the batch wait.

> **KNOWN LIMIT — the partial-fill residual (verify this before you size up).** A
> `partially_filled` order is materialized **once**, at its `filled_avg_price` with
> `qty = filled_qty`, and the residual (unfilled) shares are **never reconciled** if the venue
> fills more later — the scope note in `pipeline/reconcile.py::reconcile_live` says so
> outright. Whole-share Alpaca **paper** fills are effectively atomic, so this is invisible on
> the Stage-0 host. A **real-money** endpoint partially fills for real, and then `qty`
> *understates* the position: every `$` breaker above multiplies by it, so
> `max_daily_loss_usd` and `max_drawdown_usd` **under-count the loss**, and the positions
> screen sizes the tile low too. Treat this as an open item, not a footnote: during the drill,
> **check whether this account's fills are ever partial at all** (D1 below) — and **re-check it
> on the live host before raising size**, since fill behaviour is a property of the venue and
> the order size, not of the code. Residual reconciliation must land before real money runs at
> a size where a partial fill is likely.

### Where you set them

- **Cockpit → Safety screen (`7`) → the guardrails panel** (between ARMING LOCKS and HARD
  CAPS). Caption: *"a brake, not a knob — env stays the master arm."* This is the normal
  path. Edits append an `edit` event to the guardrail history.
- **Headless/local:** `db.guardrails_repo.edit_limits(session, source=..., **limits)` — it
  whitelists the limit columns only (a limit edit can never move the state columns; editing
  a cap while tripped leaves the brake tripped) and rejects a non-positive value.

### The trip protocol, from your seat

A breaker breach is evaluated at three points: the morning digest's dispatch loop, the
evening screen (after its live reconcile), and the hourly intraday-exit job — one shared
`pipeline/guardrails.py::consult`. When one breaches:

1. **The brake persists first** — `state='tripped'` + a `trip` event, committed *before*
   anything touches the venue. The brake holds even if everything after it dies.
2. **The sweep runs** — the proven disarm sweep: pull the **entry-side (buy) resting
   orders**, verify/restore every open position's protective stop at its recorded level.
   It **never flattens a position.** A `DisarmEvent(reason='guardrail:<breaker>')` is
   written so the System Behavior Auditor grades it as sanctioned conduct.
3. **You get an email** — `Swing Screener — GUARDRAIL TRIPPED: <breaker> (<run_date>)`, one
   per trip (`EmailLog(kind='guardrail')`, deduped on the trip event id). The reason (with
   its dollar figures) leads the body, so a lock-screen preview already answers "why".
4. **The Safety screen banner** reads `TRIPPED — <reason>` once the sweep is done. While
   it is **not** done the headline changes to `TRIPPED — SWEEP RETRYING` — the unfinished
   sweep *replaces* the reason in the headline (the venue may still hold working entry
   orders, which outranks the breaker's own text) and the reason drops to the line below.
   A `pending`/`partial` sweep means **retrying**, not failed: every subsequent
   digest/screen/hourly cycle re-runs it (stop restoration skips already-protected
   symbols, so re-runs are safe), and the cockpit's **DISARM** button resumes that same
   sweep. Only `sweep_state='complete'` earns the "entry orders were pulled, protective
   stops kept" sentence.
5. **Clearing is yours, and it is deliberate**: tick the acknowledge box (which is what
   enables the hold), then a 900 ms hold. Re-arming is never automatic.

> **Clearing a still-breached drawdown re-trips.** Clearing does **not** reset the
> high-water anchor. If equity is still below the line, the next cycle (within the hour,
> via the intraday-exit job) trips again — sweep and email included. That is correct: the
> drawdown is still real. The clear dialog says so and offers the **anchor reset** beside
> it; resetting the anchor (a fresh budget, e.g. after a deposit) is its own explicit
> `edit` action with its own event.

### MODE-OFF CAVEAT (read twice)

**While a trip's sweep is unfinished, `SWING_EXECUTION_MODE=off` no longer guarantees zero
venue writes.** Every digest and every hourly cycle checks for a tripped book with
`sweep_state` `pending`/`partial` and, if it finds one, **builds a broker on demand
regardless of the execution mode** and finishes the sweep
(`notify/run.py`'s once-per-run resume, `pipeline/exitcheck.py`'s hourly pass).

This is deliberate. The natural operator reaction to a trip is to flip the mode off, and
before this the flip would strand resting DAY entry orders fillable at the venue with
nothing retrying. **The sweep only ever reduces exposure** — it cancels buy-side resting
orders and restores protective stops; it opens nothing and closes no position. Once
`sweep_state` reads `complete`, mode-off is once again total silence.

### The nightly stop re-assert (not a disarm)

Bracket entries go out `time_in_force='day'`, and the venue **may** apply that TIF to the
child stop leg — which would kill the stop at the close and leave a multi-day swing hold
naked overnight. Rather than guess, the **evening screen re-asserts the invariant every
night**: any open live position with no live stop at the venue gets a plain **GTC** stop
re-submitted at the `ExecutionLog` ticket's **recorded** level (copied, never computed);
a position with no recorded level is logged `UNPROTECTED` for you and **left alone**.

Three things to know about it:

- It runs `ensure_stop_protection` **only** — it does **not** pull resting entry orders. A
  healthy book's working entries survive the evening screen; pulling entries is a
  trip/halt/kill *response*, not a nightly invariant.
- It **writes no `DisarmEvent`** — nothing was disarmed. This is an invariant *repair*; the
  `evening re-assert: …` log lines are its record. (Trip/halt/kill sweeps journal a
  `DisarmEvent` precisely because they cancelled entry orders.)
- It is skipped for the one evening the guardrails consult already swept in that same run
  (one pass per evening, so the conduct record isn't double-logged). A `halted` or
  `tripped` book that swept nothing this evening still gets the re-assert — the brake is
  about not opening new risk, never about abandoning protection on what is already on.

---

## The ceremony (ordered)

Do these in order. Steps 1–8 are checks, configuration, and a drill — none of them arms
anything; only step 9 flips the switch.

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

> **Ordering hazard — credentials before anything else.** `build_broker` resolves the key
> and secret through `require_secret`, which **raises** when they are missing, and four
> call sites are **unguarded**:
>
> - the two **job** sites — `notify/run.py` (the digest's live-broker build) and
>   `pipeline/run.py` (the screen's). With `SWING_BROKER=alpaca` and
>   `SWING_EXECUTION_MODE=live` but no credentials resolvable, those jobs **crash before
>   sending anything** — no digest, no screen, no email telling you why.
> - the two **CLI** sites — `preflight.main` and `disarm.main`. So step 7 run before the
>   secrets exist gives you a **traceback, not a `NO-GO`**, and the emergency `disarm` CLI
>   is equally unavailable. A traceback here is a missing-secret symptom, not a preflight
>   verdict; don't read it as one.
>
> Always create the credentials first. On Azure this means: **create the Key Vault secrets
> before `executionMode` goes to `live`** (see the appendix), and before you lean on
> preflight or `disarm` against the armed config.

### 3. Set ALL caps + the allow-real-money flag (lock 2 + the caps)

```bash
export SWING_MAX_DAILY_NOTIONAL=<max $ notional/day, e.g. 5000>
export SWING_MAX_DAILY_LOSS=<per-day loss breaker in R, e.g. 2.0>
export SWING_MAX_CONCURRENT=<max open live positions, e.g. 3>
export SWING_BROKER_ALLOW_REAL_MONEY=yes
```

All three caps are mandatory — omit any one and a real-money order is refused
(`rejected_live`). `SWING_BROKER_ALLOW_REAL_MONEY=yes` is the explicit, loud second lock;
keep it **unset** until you genuinely mean to arm.

> **Orientation, if you are deployed on Azure** (you are): these `export`s configure
> **your shell** and reach nothing remote. The Azure jobs already carry the three caps from
> `infra/main.bicepparam` (`maxDailyNotional` / `maxDailyLoss` / `maxConcurrent`, currently
> sized to the $1,000 starter account) — so for the jobs this step is already done, and
> changing it is a bicepparam edit + re-provision. The local exports matter for the
> read-only CLIs you run next (`preflight` reads *this* process's env) and for a local
> drill. The same split applies to every `export` in this ceremony; the appendix is where
> the Azure half actually happens.

### 4. Set the three MANDATORY breakers — before the live flip

All three of `max_daily_loss_usd`, `max_trades_per_day` and `max_drawdown_usd` must be set
**before** step 9. This is not advice: preflight enforces it (step 7 reads `NO-GO` on a
real-money host while any of them is unset), and so does the adapter (`rejected_live`, no
broker order). `loss_streak_halt` is optional — set it if you want it.

Set them on the **Safety screen's guardrails panel** (cockpit, `7`), or headlessly via
`db.guardrails_repo.edit_limits`. Also confirm the panel banner reads **OK** — an engaged
brake (`halted`/`tripped`) is a `NO-GO` on **any** host, paper included.

Size them to the funded account, not to ambition: they are dollar amounts, and the env
caps in step 3 are a *different*, R-denominated net sitting underneath.

### 5. Set the execution scope — `SWING_EXECUTE_PLAY_TYPES`

```bash
export SWING_EXECUTE_PLAY_TYPES=reversal      # the intended strategy, explicitly
```

**Unset means ALL play types dispatch — including `continuation`.** That is the 2026-07-18
audit finding worth naming: `continuation` has **no confirmed edge**
([edge/continuation.md](../../edge/continuation.md): every measured slice sits at or below
breakeven net of cost), while `reversal` is the confirmed one. Arming with the scope unset
therefore puts real money behind a strategy the evidence does not support. Set it
explicitly to what you mean to trade.

Behavior as built (`settings._parse_play_types`): unset **or blank** → allow-all (today's
behavior); a valid list → only those play types dispatch, the rest get a `skipped` ticket
(`play type not in execution scope`) in the digest. An **unknown member is dropped** with a
loud warning and the **valid members survive** (`"reversal,typo"` → `{reversal}`); only when
*nothing* valid survives does it collapse to the **empty set = allow-none**, again loudly.
Garbage never widens scope. Valid members are `continuation` and `reversal`. The knob is
visible on the cockpit's read-only CONFIG panel.

> This one has a bicep param (`executePlayTypes`) — see the appendix. It is the knob that
> makes path A of the ceremony able to scope execution at all.

This env value is the **ceiling**, not the final word: the cockpit can *subtract* from it
(`agent_guardrails.disabled_play_types`, resolved by
`guardrails_repo.effective_execution_scope`), one-click disabling a strategy without an env
change. It can only narrow, and re-enabling is capped at this ceiling — same algebra as
releasing HALT. So set the env to the widest set you would ever want traded, and use the
cockpit to tighten day to day.

### 6. Fund the Alpaca account

Fund (and ACH-clear) the live Alpaca account so it is `ACTIVE` with `buying_power > 0`.
Preflight's funding check is critical and will read `NO-GO` against an empty/unfunded account.

### 7. Run preflight → confirm **GO**

The read-only go/no-go check. It reads the broker, the settings snapshot, the guardrails
brake row, and the gate — and **writes nothing, arms nothing** (the brake read is
`peek_guardrails`, a plain column select that never even seeds the row):

```bash
SWING_EXECUTION_MODE=paper python -m swing_screener.pipeline.preflight
```

> **Run it under `paper`, not `off` — and do NOT set `live` to make it pass.** The `config`
> check requires a mode that actually trades, so from the default `off` you get
> `NO-GO: config — execution_mode is 'off' (not paper/live)` no matter how correct
> everything else is. That is expected, not a problem to fix by arming: **`paper` is not
> arming** — with the paper host it is fake money, and even against the live host `paper`
> resolves to the PaperAdapter, which never reaches a broker. Preflight is the check you
> run *before* step 9, so give it `paper` and read the other six lines.

Confirm the verdict line reads **`GO`**. The critical checks (all must pass for GO) are
`config` (broker selected + mode is paper/live), `reachable` (`get_account` succeeds),
`funded` (`ACTIVE` + `buying_power > 0`), `caps` (all three env caps set), and
`guardrails` — whose criticality is a **split**:

- an **ENGAGED brake** (state `halted` or `tripped`) is critical on **any** host, paper
  included: with the brake on, the agent will not trade at all, so a "GO" would be a lie;
- an **unset mandatory breaker** is critical on a **real-money host only** — the mandate
  sits inside execution's `is_real_money()` block, so the paper sandbox is exempt exactly
  as it is from the caps mandate. With no broker resolvable, real-vs-paper is unknown and
  that half stays advisory.

Either way the `guardrails` line's ✓/✗ reports the **real** mandate answer on every host,
so an unset breaker is visible on paper *before* the flip. The detail names the trip id +
reason when tripped, and tags an unset breaker `[brake setting — cockpit guardrails, not
env]` so it can't be confused with the near-identical caps line.

`is_real_money` and `autonomy_gate` are **advisory** lines (a heads-up, never flipping GO)
— but you should see `is_real_money: live host -> REAL money` and `autonomy_gate: autonomy
gate READY` here, consistent with steps 1–2. A `NO-GO` names the failing critical check;
fix it and re-run.

> **ENV-SCOPE CAVEAT — whose readiness is this?** Preflight reads **this process's**
> environment. `config`, `caps`, `reachable`, `funded` and `is_real_money` therefore
> describe *your shell*, not the Azure jobs: a `GO` locally says nothing about whether the
> jobs carry the same broker, host, credentials or caps. Only two lines are **shared
> state** every process sees — `guardrails` (the `agent_guardrails` row) and
> `autonomy_gate` (the scored-call book + verdict sidecars). To check the jobs themselves,
> read their actual env (`az containerapp job show --name <job> -g <rg>`) or the cockpit's
> Safety screen, which labels exactly this split with `env_scope`.

### 8. Kill-switch drill (rehearse the abort before you arm)

Before arming for real, rehearse the disarm. With a live adapter wired, the dispatch loop
**re-reads `SWING_EXECUTION_MODE` before every submit** (`notify/run.py::_execution_halted`):
if it is no longer `live`, the loop **halts** the rest of the batch AND pulls the
**entry-side (buy) resting orders** — never a blanket cancel, which would strip the
bracket's protective stop legs off open positions. Any position whose stop leg is found
dead gets a plain GTC stop re-submitted at the ExecutionLog ticket's recorded level
(copied, never computed); a position with no restorable level is reported `UNPROTECTED`.

> **You cannot rehearse this by typing `export`.** The re-read is of the **running
> process's own environment**, and a process's env cannot be mutated from another shell —
> `export SWING_EXECUTION_MODE=off` in your terminal does not reach the digest that is
> already running, and on Azure a `job update` reaches the *next* execution, never a live
> replica. The mid-dispatch abort you can actually exercise is the cockpit **HALT**: it is
> a DB row every process re-reads per intent. **Rehearse it as
> [D2](#d2--forced-halt-mid-dispatch-the-1-order-leak-bound) in the Stage-0 drill** — the
> assertions are the same ones (batch stops, entry orders pulled, every position still stop
> protected, ≤1-order leak bound), and D2 runs them against fake money.

What you *can* verify here without a running batch: that a mode already `off` at process
start submits nothing (start a run and confirm the dispatch loop is skipped entirely), and
that the standalone sweep works — `python -m swing_screener.pipeline.disarm --dry-run`,
then for real. Note the asymmetry that makes this necessary: a mode `off` at start means
the dispatch loop never runs, so **nothing is submitted *and nothing is swept*.** Setting
the mode off between runs is a valid disarm of *future* submits only; pulling resting
entries is the cockpit **DISARM** button's job, or that CLI's (see Rollback).

Satisfy yourself both halves work **before** any real order rests at the venue.

### 9. Flip `SWING_EXECUTION_MODE=live` — arm

This is the single act that arms. With locks 1–2, the caps, the three mandatory breakers,
the execution scope, funding, and a `GO` preflight all in place, and lock 3
(`autonomy_gate.ready`) earned:

```bash
export SWING_EXECUTION_MODE=live
```

The next dispatch run submits one live order per pick (idempotent per pick/run/side), records
the `broker_order_id` to the `ExecutionLog`, and opens **no** position at submit (the
reconciler materializes the position from the broker's eventual fill).

> **On Azure this `export` arms nothing.** It arms the shell you are sitting in. The Azure
> jobs are armed by the appendix — path A (`executionMode = 'live'` in `main.bicepparam`
> plus a re-provision) or path B (`az containerapp job update … SWING_EXECUTION_MODE=live`
> on the three broker-touching jobs). Until one of those runs, the deployed cadence stays
> on whatever `main.bicepparam` last set (today: `paper`).

### 10. Monitor

- **The digest** — the autonomy-gate status line and the proposed/recorded orders.
- **The `ExecutionLog`** — one row per submit: `submitted_live` (working at the venue, with
  `broker_order_id`), `skipped` (a hard-limit clamp, or a `guardrail: …` brake refusal), or
  `rejected_live` (a lock/cap/mandate refusal or a broker error). This audit table is the
  single source of truth for every live order.
- **Alert emails** — three kinds now land: exit alerts (the intraday exit cadence),
  **guardrail trip** alerts (`EmailLog(kind='guardrail')`, one per trip), and **live
  rejection** alerts (`rejected_live` rows only — `canceled` is deliberately *not* alerted:
  it covers benign end-of-day expiry and the system's own sweep-cancels, and paging you to
  re-enter orders the guardrails just killed would be exactly wrong). The hourly
  intraday-exit job is the at-least-once retry owner for rejection alerts.
- **The Safety screen** (`7`) — brake banner + breach line. Whenever the brake is engaged
  the masthead shows a `HALTED`/`TRIPPED` chip beside the execution-mode chip (it is absent
  while the brake reads `ok`), clickable straight through to the Safety screen.

## Rollback / kill switch

To disarm at any time:

```bash
export SWING_EXECUTION_MODE=off
```

`execution_mode` is **re-read every run** (and re-read before every submit inside a run), so
setting it to `off` (or anything other than `live`):

1. stops the **next** order from being submitted, and
2. **when the flip lands mid-run**, pulls the **entry-side (buy) resting orders** and
   verifies every open position still holds its protective sell stop, restoring a dead stop
   leg at the ExecutionLog ticket's recorded level (the per-submit kill switch in
   `notify/run.py`). The bracket stop and target legs of open positions are **left
   working** — they are the protection.

If the mode is already `off` when the process starts, the dispatch loop is skipped
entirely: nothing submits, but nothing sweeps either. To pull resting entries on an
already-disarmed book, use one of:

- the cockpit's **DISARM** button (Safety screen / masthead), or
- `python -m swing_screener.pipeline.disarm [--dry-run]` — the same entry-cancel +
  stop-verification sweep on demand, reporting any position it had to leave `UNPROTECTED`
  (no live stop and no recorded level to copy). It is mode-independent: it only needs
  `SWING_BROKER` and credentials.

Setting `off` is the safe, reversible disarm — it is exactly what the step-8 drill
rehearses. A working live order that has already **filled** is a position; reduce/close it
through Alpaca directly (the reconciler will then record the exit).

**Remember the mode-off caveat above:** a tripped book with an unfinished sweep keeps
retrying that sweep every cycle even with the mode off, until `sweep_state` reads
`complete`. The sweep only reduces exposure.

---

## AZURE APPENDIX — arming the Container Apps jobs

Two mechanisms reach the ten Azure jobs, and **they must be kept in agreement**. Read the
pairing rule before you use either.

Placeholders follow [azure-deploy.md](../azure-deploy.md)'s convention: `<rg>` is the
resource group, `<region>` the location, `<vault>` the Key Vault name.

### 0. FIRST: create the Alpaca secrets in Key Vault

Do this **before** anything sets `executionMode=live` (see the ordering hazard in step 2 —
the unguarded `build_broker` sites crash the digest and the screen outright when the mode is
live and the credentials don't resolve).

No bicep change is needed. `config_secrets.get_secret` maps an env **name** to a vault
secret name by lower-casing and replacing `_` → `-`, and every job already carries
`KEY_VAULT_URL` with the UAMI holding *Key Vault Secrets User*. Creating the three secrets
is sufficient.

The vault name is **generated** by `main.bicep` (name prefix + a resource token), so look it
up rather than guessing:

```bash
VAULT=$(az keyvault list -g <rg> --query "[].name" -o tsv)
echo "$VAULT"        # sanity-check: exactly one name, not an empty string or a list
```

```bash
az keyvault secret set --vault-name "$VAULT" --name swing-alpaca-key    --value <LIVE Alpaca key id>
az keyvault secret set --vault-name "$VAULT" --name swing-alpaca-secret --value <LIVE Alpaca secret>
az keyvault secret set --vault-name "$VAULT" --name swing-alpaca-host   --value https://api.alpaca.markets
```

Do **not** add them to `keyvault.bicep`'s seeded secrets or to the jobs' `secretDefs`:
seeding would mean three more `@secure()` params on *every* deploy (and a `seedSecrets=true`
run would blank the real values), and a `secretDefs` entry would make all ten job resources
**fail to provision** until the vault secrets exist. Runtime fetch avoids both.

### A. CANONICAL — bicep params + a re-provision (drift-safe)

`infra/main.bicepparam` carries `broker`, `allowRealMoney` and `executePlayTypes`
**commented out** so a routine re-provision keeps every job disarmed and unscoped.
Uncommenting them *is* the arming ceremony:

```bicep
// infra/main.bicepparam -- UNCOMMENT these three (they exist only as comments today):
param broker = 'alpaca'
param allowRealMoney = 'yes'          // real money. Read this runbook first.
param executePlayTypes = 'reversal'   // WHICH strategy. Unset = ALL, incl. continuation.
```

…and then **change the `executionMode` line that already exists** (near the top of the
same file, currently `param executionMode = 'paper'`) to `'live'`. Do **not** add a second
`param executionMode` line — a duplicate assignment in a `.bicepparam` is a compile error,
so the ceremony would fail at `az deployment sub create` rather than arm anything.

`executePlayTypes` is what makes this path able to scope execution at all: without it a
drift-safe re-provision is structurally allow-all, i.e. it would put real money behind
continuation too (step 5).

Then re-provision by hand — **CD never applies bicep** (`cd.yml` only repoints job images),
so nothing in that file reaches Azure until you run:

```bash
az deployment sub create \
  --location <region> \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam \
  --parameters seedSecrets=false \
               anthropicApiKey=<...> digestTo=<you@example.com> \
               alertEmail=<you@example.com> \
               sqlAadAdminLogin=<your-upn> sqlAadAdminObjectId=<your-object-id> \
  --what-if      # review first, then re-run without --what-if
```

**`seedSecrets=false` is mandatory on every re-deploy**, or the live vault values are
overwritten by the empty placeholders in the param file.

This path is **drift-safe**: the param file is the record, so the next re-provision
reproduces the armed state instead of silently undoing it. It applies the broker env to
**all ten jobs** uniformly (the params feed `commonEnv` in `infra/modules/jobs.bicep`).

### B. FAST PATH — per-job `az containerapp job update` (survives CD, dies on re-provision)

This path arms **three** of the ten jobs, and the two it leaves out are the ones to
understand before you choose it:

| Job | Entrypoint | Broker role under this path |
|---|---|---|
| `evening-screen` | `pipeline.run` | **armed** — live reconcile, the guardrails consult, the nightly stop re-assert |
| `daily-digest` | `notify.run --kind daily` | **armed** — the dispatch loop; this is the one that **submits** |
| `intraday-exit` | `notify.run --kind exit` | **armed** — hourly live reconcile, sweep resume, rejection-alert retries |
| `weekly-digest` | `notify.run --kind weekly` | **NOT armed** — but it runs the *same* dispatch loop and would submit if it were. Left on `executionMode='paper'` from bicep, it takes the **PaperAdapter**: simulated fills into the `account="paper"` book. No venue, no dollars — but also *not* a no-op, and its tickets will read `paper`, not `skipped` |
| `monthly-digest` | `notify.run --kind monthly` | **NOT armed** — same as weekly (last business day of the month) |

The remaining five (`on-demand-analysis`, `market-weather`, `journal-coach`,
`journal-audit-weekly`, `journal-audit-breach`) never dispatch and need no broker at all.

So the fast path creates a **weekend/month-end divergence**: Friday's and the month-end
digest go to paper while the weekday cadence goes live. It is safe (paper moves no money),
but it is a real difference from path A, which arms all ten uniformly. **Scoping to
`reversal` (step 5) makes the divergence moot for continuation** — weekly/monthly build
continuation picks only, so with the scope set they have nothing in scope to dispatch
either way. Use path A if you want the weekly/monthly cadences genuinely trading.

```bash
# 1) broker + the loud flag + the scope ceiling (inert on their own -- mode is still not live)
for job in evening-screen daily-digest intraday-exit; do
  az containerapp job update --name "$job" -g <rg> \
    --set-env-vars SWING_BROKER=alpaca SWING_BROKER_ALLOW_REAL_MONEY=yes \
                   SWING_EXECUTE_PLAY_TYPES=reversal
done

# 2) LAST, and only after the vault secrets exist: the arm
for job in evening-screen daily-digest intraday-exit; do
  az containerapp job update --name "$job" -g <rg> --set-env-vars SWING_EXECUTION_MODE=live
done
```

`job update` edits the job **template**, so the change survives CD's image repoints (CD
updates the image, not the env).

> **It is silently stripped by any re-provision.** `az deployment sub create` rewrites the
> job templates from bicep, and with `broker` / `allowRealMoney` / `executePlayTypes` still
> commented out in `main.bicepparam` every one of those template defaults is `''` = ABSENT
> — the CLI-set vars vanish with no error and no log line. A job that was armed becomes
> disarmed (fail-safe, but *silent*).
>
> The scope var is the nastier half: `SWING_EXECUTE_PLAY_TYPES` disappearing does **not**
> disarm anything, it **widens** the scope back to allow-all. Pair that with an
> `executionMode` still set to `live` in the param file and a re-provision would arm
> continuation. Which is exactly why:
>
> **THE PAIRING RULE: if you flip via CLI, update `infra/main.bicepparam` in the same act —
> or never re-provision while armed.** Pick one. It now covers all four settings (broker,
> allowRealMoney, executePlayTypes, executionMode), and the bicep comments say the same
> thing from the other side.

### Disarming on Azure

**Mode off first, then remove the broker vars** — the reverse order would leave the jobs in
`live` mode with no broker (a warn-and-NoOp state that reads confusingly in the logs):

```bash
# 1) disarm
for job in evening-screen daily-digest intraday-exit; do
  az containerapp job update --name "$job" -g <rg> --set-env-vars SWING_EXECUTION_MODE=off
done

# 2) then, once the book is quiet, drop the broker wiring
for job in evening-screen daily-digest intraday-exit; do
  az containerapp job update --name "$job" -g <rg> \
    --remove-env-vars SWING_BROKER SWING_BROKER_ALLOW_REAL_MONEY SWING_EXECUTE_PLAY_TYPES
done
```

(Dropping `SWING_EXECUTE_PLAY_TYPES` last is safe here precisely because step 1 already
disarmed: with no broker and no live mode, a widened scope has nothing to widen.)

Then re-comment the params in `main.bicepparam` (the pairing rule, in reverse). Remember
that mode-off does not sweep: pull resting entries with the cockpit **DISARM** button.

### Kill-switch drill, ACA flavor

```bash
az containerapp job update --name daily-digest -g <rg> --set-env-vars SWING_EXECUTION_MODE=off
az containerapp job execution list --name daily-digest -g <rg> -o table   # confirm the next run
```

- An env change applies to the **next execution**. Azure cannot change a running replica's
  environment, and the app reads the mode from its own process env — so `job update` is a
  *between-runs* control. **The cockpit HALT is the only stop that reaches a job
  mid-dispatch** (it is a DB row every process re-reads per intent); that is precisely why
  the brake exists.
- **NEVER `az containerapp job start --env-vars …`.** That replaces the execution's env
  block wholesale and **drops the Key Vault `secretRef` entries** — the run comes up with no
  `ANTHROPIC_API_KEY`, no `DIGEST_TO`, no DB URL. Use `job update` (which edits the
  template, preserving `secretDefs`) and then `az containerapp job start --name <job> -g <rg>`
  with no env override.

---

## STAGE-0 DRILL — the paper-host rehearsal (do this before real money)

Run the whole machine against the **Alpaca paper sandbox**
(`SWING_ALPACA_HOST=https://paper-api.alpaca.markets`) with `SWING_EXECUTION_MODE=live`.
This is not a paper-*mode* run: it is the **live plumbing** pointed at fake money, so every
brake path is exercised for real. The submit-side brake deliberately sits **outside** the
`is_real_money()` gate, so it fires on the paper host too — that is what makes this drill
meaningful rather than a no-op.

Preflight first: expect `GO` with `is_real_money: paper host -> fake money (paper)`.

**How to start a dispatch cycle on demand** (D2 and D3 both need one):

```bash
# local: run the daily digest's dispatch loop directly
python -m swing_screener.notify.run --kind daily
```

```bash
# Azure: force the hour gate open, fire the job, then remove the override
az containerapp job update --name daily-digest -g <rg> --set-env-vars RUN_GATE_FORCE=1
az containerapp job start  --name daily-digest -g <rg>
az containerapp job update --name daily-digest -g <rg> --remove-env-vars RUN_GATE_FORCE
```

(The `RUN_GATE_FORCE` idiom is [azure-deploy.md](../azure-deploy.md)'s — `job update` +
`job start`, never `job start --env-vars`.)

### D1 — Bracket child-leg TIF (the open question Task 19 hedges)

Verbatim from the Task-19 handoff:

> Submit one paper bracket day entry, let it fill, record `list_open_orders` before the
> close and after the next open; legs gone/expired = the venue inherits `day` on children
> → entry-TIF becomes a live strategy decision; survives = the nightly re-assert stays
> belt-and-braces; either way confirm the evening re-assert logged and a same-evening
> re-run produced no duplicate stop.

Concretely: submit one bracket day entry, let it fill, snapshot `list_open_orders` **before
the close** and **after the next open**.

- **Legs gone/expired** → the venue inherits `day` on the children. Flipping the *entry*
  bracket to `gtc` then becomes a live **strategy** decision (deliberately out of scope of
  the guardrails work), and the nightly re-assert is the thing keeping holds protected in
  the meantime.
- **Legs survive** → the nightly re-assert stays belt-and-braces, and nothing needs to change.

Either way: confirm the **evening re-assert logged** (`evening re-assert: re-submitted …`
or a clean no-op), and that a **same-evening re-run produced no duplicate stop** — the
`screen-<YYYYMMDD>` `key_suffix` day-stamps the restore client_order_ids, so the venue's
duplicate-id rejection makes the second pass idempotent. A duplicate live GTC sell stop on
a margin account closes the position and then shorts it; this is the check that proves it
can't happen.

**And while you have a fill in front of you — the partial-fill check** (the residual limit
above): record the order's `filled_qty` against the ticket's requested `shares`, and the
materialized `PaperTrade.qty` against both. **Any** pass through `partially_filled` — even
one that later completes — is the finding: it means this account's fills are *not* atomic,
the residual never reconciles, and the `$` breakers would under-count on the live host.
Write the answer down; it is the precondition for sizing up.

### D2 — Forced HALT mid-dispatch (the ≤1-order leak bound)

Start a cycle (above) with several intents to dispatch, and while it is running press
**HALT** on the cockpit's guardrails panel. The brake is a DB row every process re-reads
per intent, so this is the *only* control that lands mid-dispatch — an env flip cannot,
which is exactly what this drill proves. Assert:

- the batch stops;
- **at most one** in-flight submit lands after the HALT commit (the accepted, bounded
  residual — it is capped, bracket-protected, and logged);
- the manual-HALT sweep ran (entry-side pulls + stop verification) and wrote a
  `DisarmEvent(reason='halt')`;
- the Safety banner reads `HALTED` and the masthead chip agrees.

Then release the HALT and confirm dispatch resumes on the next cycle.

### D3 — Forced trip with a tight limit

**Prerequisite for the drawdown breaker:** it is computed from *realized* dollars —
`(exit − entry) × qty` over **closed** live trades, ordered by `ExitEvent.id`, since the
anchor. A fresh paper book has none, so `max_drawdown_usd` set on day one simply reads 0
and never fires. You need **at least one closed, `qty`-stamped live losing trade** first
(D1's bracket entry, stopped out, reconciled). Legacy rows with a NULL `qty` contribute 0
and are never guessed at.

So run this in two passes:

1. **The deterministic forced trip — `max_trades_per_day = 1`.** This breaker counts
   counting-status live `ExecutionLog` rows for the run date, so it needs no closes at all
   and fires on the *second* order of any cycle. Set it on the Safety panel, start a cycle
   with ≥2 intents, and walk the flow below (substituting `max trades/day: …` for the
   reason). This is the one to use if you want the trip on demand.
2. **The drawdown pass — once a real closed loss exists.** Set `max_drawdown_usd` below the
   book's realized drawdown and let the next cycle run. Only this pass exercises the
   anchor-reset remedy, which is why it is worth doing rather than skipping.

Either way, walk the whole flow:

- **sweep** — entry-side orders pulled, stops verified/restored, `DisarmEvent(reason=
  'guardrail:<breaker>')` written;
- **email** — exactly one `GUARDRAIL TRIPPED` mail (re-runs must not re-send: the
  `EmailLog(kind='guardrail')` key is the trip event id);
- **banner** — `TRIPPED — <reason>` on the Safety screen; if the sweep hasn't finished the
  headline instead reads `TRIPPED — SWEEP RETRYING` with the reason on the line below
  (then confirm the next cycle retries it and the headline flips back to the reason once
  `sweep_state` reaches `complete`);
- **clear flow** — the ack checkbox gates the 900 ms hold, and clearing works;
- **re-trip warning** — with the breach *still* active, confirm the clear dialog says so
  and names the per-breaker remedy, then confirm it **does** re-trip on the next cycle.
  The remedy differs: on pass 1 the count only resets at the next trading day of record
  (or raise the cap); on pass 2 the dialog additionally offers the **anchor reset** beside
  the clear — take it, and confirm the book then stays released.

Also confirm the brake blocks submits while engaged: a dispatch attempt while tripped must
produce `skipped` `ExecutionLog` rows with a `guardrail: …` detail and **no** broker call.

### D4 — G7 grant verification (cockpit POST works)

The cockpit **writes** (guardrail state, guardrail events, `DisarmEvent`, journal, audit
ACKs), so the Entra principal it runs as needs `db_datawriter` — `infra/post-deploy.sql`,
run once by the Entra admin ([azure-deploy.md Step 3](../azure-deploy.md)). This is a
**manual prod step** and it is easy to forget.

Verify by exercising a real cockpit write against the prod DB — a guardrail limit **edit**
is the cheapest one (pure DB, no venue call). It must return **200** and the new value must
survive a refresh. A missing grant surfaces as a **503** (`database error (…)`), never a
silent 200: the guardrails endpoints deliberately let `SQLAlchemyError` propagate on the
primary state write, precisely so a missing grant can't drop a brake on the floor. Also
exercise **HALT** + clear once, end-to-end, since those take the same write path plus the
event append.

---

## What stays manual

- **Robinhood is a human-approval surface, never headless.** The Phase-5
  [feasibility spike](../plans/2026-06-21-phase5-approval-arming-design.md#the-spike-that-shaped-this-phase)
  found headless Robinhood auth a **NO-GO** (desktop-only OAuth, undocumented refresh-token
  lifetime, a datacenter-IP lockout risk). Robinhood holds **no stored credentials and makes
  no API call** here — you act on the "Proposed orders" list by hand. Alpaca is the only
  high-confidence headless real-money path.
- **Arming is always a human act.** Nothing in the screener flips `execution_mode` to `live`
  for you — not the gate, not preflight, not CD, not the cockpit. Promotion to real money is
  a deliberate manual step, performed here, by you, once the gate is `ready`.
- **Re-arming after a trip is a human act too.** The brake never clears itself; clearing is
  an acknowledge-then-hold in the cockpit, and a still-breached cumulative breaker will
  simply trip again until you fix the underlying condition (or explicitly re-anchor).

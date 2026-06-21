# Phase 4 — the live broker arc (Alpaca paper, armable-when-ready) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stand up the live broker path — submit → fill → **reconcile** → exit against a real broker API — and prove the whole order lifecycle on **Alpaca's paper sandbox** (zero real money, headless), with the real-money safety layer built-but-inert so a future live flip is a guarded config change.

**Architecture:** A `BrokerClient` Protocol (injected like `smtp_send`; `FakeBroker` in tests) with an `AlpacaBroker` (httpx REST, paper endpoint). A `LiveAdapter` submits + records `broker_order_id` but opens **no** position. A `reconcile_live` polling pass materializes the `account="live"` `PaperTrade` from the **broker's** fills (the broker owns truth) and is idempotent on `broker_order_id`; the bar-stepper **excludes `account="live"`** (two disjoint engines). Real money needs three independent locks (`execution_mode=live` AND `SWING_BROKER_ALLOW_REAL_MONEY=yes` AND `autonomy_gate.ready`) + mandatory caps + a per-submit kill switch — all inert on Alpaca paper.

**Tech Stack:** Python 3.12, httpx (already a dep), SQLAlchemy + Alembic, pytest. No new deps.

**Governed by:** [North Star](../NORTH_STAR.md) (#1/#3/#4/#6 + kill switch) + the [Phase-4 design](2026-06-21-phase4-live-broker-design.md). Builds on Phase 0-3 (#41/#43/#45/#47/#48).

---

## Design invariants (hold for every task)
- **Money never auto-moves.** Default `execution_mode="off"`. A real-money endpoint is reachable only with all three locks (mode=live AND `SWING_BROKER_ALLOW_REAL_MONEY=yes` AND `autonomy_gate.ready`). **Alpaca paper needs none of them** — this whole phase runs money-safe.
- **The broker owns the fills.** The live adapter opens NO position at submit; `reconcile_live` materializes it from the broker's real fill price. The bar-stepper must NEVER touch an `account="live"` row.
- **Idempotency.** `client_order_id = idempotency_key` (the broker rejects dupes); `reconcile_live` keys on `broker_order_id` (materialize each fill once).
- **No live network in CI.** `FakeBroker` drives every adapter/reconcile test; the `AlpacaBroker` REST client is tested against recorded/fake JSON responses only.
- **Levels are deterministic** (North Star #4): broker orders are placed at the rules-engine `limit_price`/stop/target, never recomputed.
- **Commit discipline:** gate on green (`.venv\Scripts\python.exe -m ruff check .`, `-m mypy`, `-m pytest -q`) BEFORE each commit. No `--no-verify`. Trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Migrations chain to a single head (`alembic heads`).

---

## Task 1: ExecutionLog broker columns + live statuses

**Files:** Modify `db/models.py` (ExecutionLog) + a migration; `db/repo.py` (`_LIMIT_COUNTING_STATUSES`); Tests.

**Step 1 — Model + migration.** Add to `ExecutionLog`: `broker: Mapped[str] = mapped_column(String(16), default="", index=True)`, `broker_order_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)`, `broker_status: Mapped[str | None] = mapped_column(String(32), default=None)`. Migration (`down_revision` = current head `04c3f9c9f905` — CONFIRM via `alembic heads`): add the three columns (`broker` `server_default=""` non-null; the other two nullable) + the two indexes. Verify applies on sqlite + single head; extend `tests/test_alembic_offline.py` index set.

**Step 2 — Statuses.** Extend `_LIMIT_COUNTING_STATUSES` (repo.py:193) to `("recorded", "filled_paper", "submitted_live", "filled_live")` — a working (unfilled) live order reserves its notional. (The full live status vocabulary `submitted_live|filled_live|canceled|rejected_live` is just strings on `ExecutionLog.status`; no enum.)

**Step 3 — TDD.** ExecutionLog round-trips the broker columns (defaults: `broker=""`, ids None); `execution_logs_for_day` counts `submitted_live`/`filled_live`; migration single-head + backfill (`broker=""`).

**Step 4 — Commit:** `feat(live): ExecutionLog broker columns + live-order statuses`.

---

## Task 2: The BrokerClient seam (Protocol + types + FakeBroker)

**Files:** Create `pipeline/broker.py`; Test `tests/pipeline/test_broker_fake.py`.

**Step 1 — Types + Protocol.**
```python
from typing import Protocol
@dataclass(frozen=True)
class BrokerOrderSpec:
    client_order_id: str; symbol: str; side: str; qty: int
    order_type: str; limit_price: float | None; time_in_force: str
@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str; client_order_id: str; status: str  # new|partially_filled|filled|canceled|rejected
    filled_qty: int; filled_avg_price: float | None; symbol: str
@dataclass(frozen=True)
class BrokerPosition:
    symbol: str; qty: int; avg_entry_price: float
class BrokerClient(Protocol):
    name: str
    def submit_order(self, spec: BrokerOrderSpec) -> BrokerOrder: ...
    def get_order(self, broker_order_id: str) -> BrokerOrder: ...
    def list_open_orders(self) -> list[BrokerOrder]: ...
    def get_positions(self) -> list[BrokerPosition]: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
    def cancel_all_orders(self) -> None: ...
    def is_real_money(self) -> bool: ...   # paper endpoint -> False
```

**Step 2 — `FakeBroker`** (the test double, in the same module or `tests/`): scriptable — a queue/dict of orders keyed by `broker_order_id`; `submit_order` assigns an id + initial status (default `new`), enforces `client_order_id` idempotency (a re-submit of the same client_order_id returns the SAME order, never a 2nd); test helpers `fill(broker_order_id, price, qty=None)`, `close_position(symbol, price)`, `reject(...)`; `is_real_money()` configurable (default False). Pure, deterministic, no network.

**Step 3 — TDD.** `FakeBroker`: submit→get round-trips; duplicate `client_order_id` is idempotent (one order); fill updates status/filled_avg_price; cancel_all clears open orders; `is_real_money()` honors the flag.

**Step 4 — Commit:** `feat(live): BrokerClient protocol + types + FakeBroker test seam`.

---

## Task 3: Broker config + the real-money locks (pure)

**Files:** `settings.py`; Test `tests/test_settings_broker.py`.

**Step 1 — Settings.** Add `broker: str` (env `SWING_BROKER`, default `""`; `alpaca` is the only impl this phase), `allow_real_money: bool` (env `SWING_BROKER_ALLOW_REAL_MONEY`, the `_TRUE` set, default False). A pure `can_arm_real_money(settings, *, gate_ready: bool) -> tuple[bool, str]`: returns `(True, "")` only if `execution_mode=="live"` AND `allow_real_money` AND `gate_ready`; else `(False, "<which lock failed>")`. (This guards a REAL-money endpoint only; an Alpaca-*paper* broker reports `is_real_money()=False` and bypasses it.)

**Step 2 — Real-money limit mandate (pure).** `real_money_limits_ok(limits) -> tuple[bool, str]`: all of `max_daily_notional`/`max_daily_loss`/`max_concurrent` must be set (not None) — a real-money order is refused with unbounded caps. (Paper is exempt.)

**Step 3 — TDD.** `can_arm_real_money`: all three locks → True; each missing → False + the right reason. `real_money_limits_ok`: all set → ok; any None → not ok. Pure, env via monkeypatch (mirror `test_settings_execution.py`).

**Step 4 — Commit:** `feat(live): broker config + real-money arming locks (pure)`.

---

## Task 4: The LiveAdapter (submit; opens no position)

**Files:** `pipeline/execution.py` (`LiveAdapter`); Test `tests/pipeline/test_execution_live.py`.

**Step 1 — Spec.** `LiveAdapter(broker: BrokerClient, *, limits, real_money_ok_fn=...)` (`name="live"`, `LIVE_ACCOUNT="live"`). `submit(intent, *, session, run_date, limits)`:
1. `key = idempotency_key(intent, run_date)`.
2. **Real-money guard:** if `broker.is_real_money()` → require `can_arm_real_money(...)` (gate_ready computed from `autonomy_gate(session).ready`) AND `real_money_limits_ok(limits)`; if either fails → `add_execution_log(status="rejected_live", detail=reason, ...)` + `OrderResult("rejected", "live", reason)`. (Paper broker skips this whole block.)
3. `_limit_block(...)` (account="live") → blocked → `skipped` + log, no submit.
4. `order = broker.submit_order(BrokerOrderSpec(client_order_id=key, symbol=intent.ticker, side="buy", qty=intent.shares, order_type="limit", limit_price=intent.limit_price, time_in_force="day"))`. On a broker exception → `add_execution_log(status="rejected_live", detail=str(e))` + `OrderResult("rejected", "live", ...)` (never raise — graceful).
5. `add_execution_log(account="live", mode="live", broker=broker.name, broker_order_id=order.broker_order_id, broker_status=order.status, status="submitted_live", side, limit_price, shares, stop, target, risk_dollars, notional, ..., idempotency_key=key)`. Return `OrderResult("submitted_live", "live", "...", broker_order_id=order.broker_order_id)`.
**Opens NO PaperTrade** (fill price unknown). Idempotent via the unique key + the broker's client_order_id.

**Step 2-4 — TDD (FakeBroker):** a submit records `submitted_live` + the broker_order_id, opens no PaperTrade; a duplicate submit doesn't double-submit (one ExecutionLog, one broker order); a blocked limit → skipped, no broker call; a `is_real_money()=True` broker WITHOUT the locks → `rejected_live`, no broker order; with all locks + caps → submits; a broker exception → `rejected_live`, graceful.

**Step 5 — Commit:** `feat(live): LiveAdapter submits orders + records broker id (opens no position)`.

---

## Task 5: reconcile_live + the stepper exclusion (the broker owns truth)

**Files:** Create `pipeline/reconcile.py`; Modify `db/repo.py` (the stepper's open-trade loader) + `pipeline/shadow.py` if needed; Test `tests/pipeline/test_reconcile_live.py`.

**Step 1 — Stepper exclusion (do FIRST, it's the safety keystone).** The bar-stepper must never advance a live row. `advance_open` (shadow.py) steps `repo.load_open_paper_trades`. Add an `account != "live"` filter to the STEPPING path — either a param `exclude_live=True` on `load_open_paper_trades` used by `advance_open`, or a dedicated `load_open_stepped_trades` (research+paper only). Keep a separate loader for reconcile. TEST: an open `account="live"` row is NOT advanced by `advance_open` (it's left untouched), while `paper`/`research` still advance.

**Step 2 — reconcile_live.** `reconcile_live(session, broker) -> int` (count reconciled). For each `ExecutionLog` row with `status="submitted_live"` (unmaterialized): `order = broker.get_order(broker_order_id)`:
- `filled`/`partially_filled` with no PaperTrade yet for this `broker_order_id` → **materialize** an `account="live"`, `status="open"`, `fill_status="filled"` `PaperTrade`: `entry_price = order.filled_avg_price` (the BROKER's price), `risk = entry_price - stop` (stop from the ExecutionLog), `stop`/`target` from the ExecutionLog, `entry_date=opened_date=today`, plus the runner-state fields a fresh fill needs (mirror PaperAdapter/shadow open). Flip the ExecutionLog `status="filled_live"`, `broker_status=order.status`. Idempotent: skip if a PaperTrade already exists for this `broker_order_id` (store it — add `broker_order_id` link, or match via the ExecutionLog→PaperTrade by pick+account; simplest: a `broker_order_id` column on PaperTrade, or a 1:1 ExecutionLog↔PaperTrade by key — DECIDE + document).
- `canceled`/`rejected` → flip the ExecutionLog status accordingly, no position.
- For OPEN `account="live"` PaperTrades, check the broker position: if the position is **gone/closed** (or a stop/target child filled) → write `exit_price`/`exit_date`/`exit_reason`/`realized_r` (from broker prices) + `record_exit_event(account="live", is_paper=False, ...)`. Idempotent (a closed row isn't re-closed).
Reconcile is **idempotent on re-poll** (keys on `broker_order_id` / `status="open"`), so running it twice never double-books.

**Step 3-4 — TDD (FakeBroker integration — the load-bearing test):** submit via `LiveAdapter` → `reconcile_live` with the broker reporting a fill → an `account="live"` PaperTrade is materialized at the BROKER's price (not the intent's); re-running `reconcile_live` does NOT double-materialize; then `FakeBroker.close_position(...)` → `reconcile_live` writes the exit + `realized_r` + an `ExitEvent(account="live", is_paper=False)`; assert `advance_open` NEVER touched the live row throughout; a `canceled` order → no position + `canceled` status.

**Step 5 — Commit:** `feat(live): reconcile_live materializes live fills from broker truth + stepper exclusion`.

---

## Task 6: The AlpacaBroker REST client (paper)

**Files:** Create `pipeline/broker_alpaca.py`; Test `tests/pipeline/test_broker_alpaca.py`.

**Step 1 — Spec.** `AlpacaBroker(BrokerClient)` (`name="alpaca"`) using **httpx** (already a dep) against Alpaca's REST API. Auth via the existing `get_secret` seam: `SWING_ALPACA_KEY`/`SWING_ALPACA_SECRET` headers (`APCA-API-KEY-ID`/`APCA-API-SECRET-KEY`), base URL from `SWING_ALPACA_HOST` (default the PAPER host `https://paper-api.alpaca.markets`). `is_real_money()` = the host is NOT the paper host. Map our types ↔ Alpaca's `/v2/orders` + `/v2/positions` JSON (submit POST `/v2/orders` with `client_order_id`, type `limit`, `limit_price`, `time_in_force=day`; `get_order` GET `/v2/orders/{id}`; `get_positions` GET `/v2/positions`; `cancel_all_orders` DELETE `/v2/orders`). Map Alpaca statuses → our vocabulary (`new`→new, `partially_filled`, `filled`, `canceled`, `rejected`). Inject the httpx client (or base_url+auth) so tests pass a fake transport.

**Step 2-4 — TDD (no live network):** use `httpx.MockTransport` (or a fake client) returning recorded Alpaca JSON: `submit_order` POSTs the right body (client_order_id, limit, qty) + parses the response into `BrokerOrder`; `get_order` parses fill fields; status mapping is correct; `is_real_money()` is False for the paper host, True otherwise; an HTTP error surfaces as a broker exception (the adapter catches it). **No real network call in any test.**

**Step 5 — Commit:** `feat(live): AlpacaBroker REST client (paper endpoint, httpx, no live network in tests)`.

---

## Task 7: Wire it into the run (adapter + reconcile cadence + kill switch)

**Files:** `notify/run.py` (`_adapter_for_mode` + the broker seam in `send_digest`), `pipeline/run.py` (run `reconcile_live` in the cadence), `pipeline/execution.py` (the per-submit kill-switch re-check + `cancel_all_orders`); Tests.

**Step 1 — Adapter wiring.** `_adapter_for_mode(mode, *, broker=None)` → `"live"` now returns `LiveAdapter(broker, ...)` (when a broker is configured) instead of NoOp+warn; `off`/no-broker still NoOp. `send_digest` gains a `broker: BrokerClient | None = None` injected seam; prod builds `AlpacaBroker` from settings when `execution_mode=="live"` + `broker=="alpaca"`. Tests inject a `FakeBroker`.

**Step 2 — Per-submit kill switch + cancel.** Before each `LiveAdapter.submit`, re-read `execution_mode` (a fresh `load_settings()` or a DB halt flag — env re-read is simplest; document) — if no longer `live`, halt remaining submits AND call `broker.cancel_all_orders()` (pull resting orders). TEST: flipping mode mid-run halts the next submit + cancels.

**Step 3 — Reconcile cadence.** In `pipeline/run.py` (the screen run, near `advance_open` at line 357), when `execution_mode=="live"` + a broker is configured, call `reconcile_live(s, broker)` each cycle (the broker threaded in as a seam, default `AlpacaBroker`, fake in tests). So fills materialize + exits reconcile every scheduled run, right where positions are advanced.

**Step 4 — TDD (end-to-end, FakeBroker):** with `execution_mode="live"` + a `FakeBroker` injected, the digest submits a live order (no position yet); the screen run's `reconcile_live` materializes the `account="live"` fill; a venue close reconciles the exit; `advance_open` never touches the live row; `off`/no-broker = today's behavior (no submit, no reconcile, NoOp). The suite never hits a real API.

**Step 5 — Commit:** `feat(live): wire the live adapter + reconcile cadence + per-submit kill switch into the run`.

---

## Definition of done
- `ruff`/`mypy`/`pytest` green; CI green; `alembic heads` single (one new migration).
- The full **submit → fill → reconcile → exit** lifecycle works end-to-end against `FakeBroker`; the `AlpacaBroker` REST client is parsed/mapped correctly against recorded responses — **no live network in CI**.
- The **broker owns the fills**: live positions are materialized by `reconcile_live` from broker prices, never simulated; the **bar-stepper never touches an `account="live"` row** (tested).
- **Money is safe:** default `off`; a real-money endpoint is refused without all three locks (`execution_mode=live` AND `SWING_BROKER_ALLOW_REAL_MONEY=yes` AND `autonomy_gate.ready`) + mandatory caps; the per-submit kill switch halts + cancels open orders. Alpaca *paper* runs without ever touching the real-money path.
- Idempotent throughout (no double-submit, no double-materialize).

## Out of scope (Phase 5+)
The **Robinhood** live adapter (a later sibling onto `BrokerClient`) + its OAuth writable-token storage (an `oauth_tokens` SQL table) + the headless/service-auth spike; native bracket/OCO orders; fractional/short/options; auto-arming (always a human act).

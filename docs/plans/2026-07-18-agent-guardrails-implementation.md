# Agent Guardrails + Stage-1 Live-Agent Fixes — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the DB-backed guardrails brake (HALT + four breakers, cockpit-writable, hard-DISARM on trip) and close the Stage-1 gaps from the 2026-07-18 readiness audit, so the first live agent can be deployed on Alpaca with one-click stop, accurate $ accounting, and same-day failure visibility.

**Architecture:** Per [2026-07-18-agent-guardrails-design.md](2026-07-18-agent-guardrails-design.md). Master arm stays `SWING_EXECUTION_MODE` (env); the brake is a single-row `agent_guardrails` table mutated only by atomic conditional UPDATEs, enforced unconditionally inside `LiveAdapter.submit` (block) and in the dispatch loop / post-reconcile hook (trip response = the proven disarm sweep). Dollar breakers become computable by stamping `qty` on live `PaperTrade` rows at fill materialization.

**Tech stack:** SQLAlchemy 2.0 mapped_column models + Alembic (head is `c1d7f3e9a5b2`), FastAPI cockpit routers, React/TS cockpit-ui (no test framework — API layer carries tests), pytest with FakeBroker fakes.

**Standing repo gotchas (apply to every task):**
- Booleans in WHERE clauses: `col == True` / string-enum columns — NEVER `.is_(True)` (sqlite hides it; SQL Server rejects `IS 1`). We avoid the trap entirely: `agent_guardrails.state` is a String enum.
- Type-check from the worktree with `MYPYPATH=src mypy src/swing_screener` (bare `mypy` checks main's editable install).
- CI runs `ruff` — run it before every push.
- Local `local.db` is create_all-born: new tables appear via `get_engine`'s create_all; the Alembic revision is the Azure SQL path. If migrating a local create_all-born DB, `alembic stamp` first.
- cockpit-ui: after ANY CSS change run a brace-balance count; verify visually via `vite dev` + Playwright MCP (in-app browser screenshots hang).
- Commit after every task with a conventional message + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

**Test commands:** `python -m pytest tests/pipeline/test_guardrails.py -v` (etc.). Full suite + `ruff check .` at every phase boundary.

---

## Phase 1 — Foundations

### Task 1: Models + migration (`agent_guardrails`, `agent_guardrail_events`, `PaperTrade.qty`)

**Files:**
- Modify: `src/swing_screener/db/models.py` (append after `DisarmEvent`, ~line 646; add `qty` to `PaperTrade` after `remaining_frac`, ~line 203)
- Create: `alembic/versions/<newid>_agent_guardrails.py` (`down_revision = "c1d7f3e9a5b2"`)
- Test: `tests/db/test_guardrails_models.py`

**Step 1: Write the failing test**

```python
"""Guardrails tables: single-row state + append-only events; PaperTrade.qty."""
from datetime import UTC, date, datetime

from swing_screener.db.models import AgentGuardrailEvent, AgentGuardrails, PaperTrade


def test_guardrails_row_defaults(session):
    row = AgentGuardrails(updated_at=datetime.now(UTC))
    session.add(row)
    session.commit()
    assert row.state == "ok"
    assert row.max_daily_loss_usd is None
    assert row.max_trades_per_day is None
    assert row.max_drawdown_usd is None
    assert row.loss_streak_halt is None
    assert row.hwm_baseline_usd == 0.0
    assert row.trip_id is None and row.trip_reason is None and row.sweep_state is None


def test_guardrail_event_requires_source(session):
    ev = AgentGuardrailEvent(
        kind="edit", breaker="", reason="set max_trades_per_day=3",
        values_json="{}", source="cockpit", created_at=datetime.now(UTC))
    session.add(ev)
    session.commit()
    assert ev.id is not None


def test_paper_trade_qty_column(session):
    pt = PaperTrade(
        account="live", ticker="AAPL", timeframe="daily", horizon="",
        play_type="reversal", signal_score=0.0, rank=0, fill_status="filled",
        status="open", stop=1.0, target=2.0, risk=0.5, qty=3)
    session.add(pt)
    session.commit()
    assert pt.qty == 3
```

Use the existing `session` fixture pattern from `tests/db/` (in-memory sqlite + `Base.metadata.create_all`). Check `tests/conftest.py` / `tests/db/` for the exact fixture name before writing.

**Step 2:** Run: `python -m pytest tests/db/test_guardrails_models.py -v` → FAIL (`ImportError: AgentGuardrails`).

**Step 3: Implement the models.** In `models.py`:

Add to `PaperTrade` (after `remaining_frac`, keep comment style):

```python
    # Live-book share count, stamped by reconcile._materialize_fills from the broker's
    # filled_qty (fallback: the ExecutionLog ticket's shares). NULL on every non-live
    # book and on legacy live rows -- realized $ math must skip NULL, never guess.
    qty: Mapped[int | None] = mapped_column(default=None)
```

Append after `DisarmEvent`:

```python
class AgentGuardrails(Base):
    """The live agent's brake state: ONE mutable row (id=1 by convention), mutated only
    by atomic conditional UPDATEs (repo.guardrails_*). Subordinate to the env master arm
    (SWING_EXECUTION_MODE): it can only BLOCK dispatch, never arm it. ``state`` is a
    String enum -- 'ok' | 'halted' | 'tripped' -- deliberately not a boolean so no WHERE
    clause ever renders `IS 1` on SQL Server. History lives in agent_guardrail_events."""

    __tablename__ = "agent_guardrails"

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="ok", server_default="ok")
    # breakers: NULL = unset. The three $-and-count breakers are MANDATORY for a
    # real-money endpoint (guardrails_mandate_ok); loss_streak_halt is optional.
    max_daily_loss_usd: Mapped[float | None] = mapped_column(default=None)
    max_trades_per_day: Mapped[int | None] = mapped_column(default=None)
    max_drawdown_usd: Mapped[float | None] = mapped_column(default=None)
    loss_streak_halt: Mapped[int | None] = mapped_column(default=None)
    # drawdown window: baseline $ at the anchor date; copied forward verbatim on every
    # edit/trip/clear -- resetting the anchor is its own deliberate 'edit' event.
    hwm_anchor_date: Mapped[date | None] = mapped_column(default=None)
    hwm_baseline_usd: Mapped[float] = mapped_column(default=0.0, server_default="0")
    # trip bookkeeping: which event tripped us, why, and whether the sweep finished.
    trip_id: Mapped[int | None] = mapped_column(default=None)
    trip_reason: Mapped[str | None] = mapped_column(String(256), default=None)
    sweep_state: Mapped[str | None] = mapped_column(String(16), default=None)
    updated_at: Mapped[datetime]


class AgentGuardrailEvent(Base):
    """Append-only guardrail history + the Auditor feed: one row per edit / halt /
    trip / clear / sweep outcome. Mirrors DisarmEvent's role for machine conduct."""

    __tablename__ = "agent_guardrail_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(16))  # edit | halt | trip | clear | sweep
    breaker: Mapped[str] = mapped_column(String(32), default="")
    reason: Mapped[str] = mapped_column(String(256), default="")
    values_json: Mapped[str] = mapped_column(Text, default="{}")
    # cockpit | digest | screen -- required, no default (journal convention).
    source: Mapped[str] = mapped_column(String(16))
```

**Step 4:** Run the test → PASS.

**Step 5: Write the Alembic revision.** `alembic revision -m "agent guardrails"` then edit; `down_revision = "c1d7f3e9a5b2"`. Docstring must carry: "Local sqlite gets these tables via get_engine's create_all; this migration is the Azure SQL path. A create_all-born local.db needs `alembic stamp` first." Ops: `op.create_table` for both tables (mirror column types above, `server_default` on the NOT NULL `state`/`hwm_baseline_usd`), `op.add_column("paper_trades", sa.Column("qty", sa.Integer(), nullable=True))`. Downgrade drops in reverse. Verify: `alembic heads` shows exactly one head.

**Step 6:** Run `python -m pytest tests/db -v` (all green) → commit `feat: agent_guardrails tables + PaperTrade.qty (design 2026-07-18)`.

### Task 2: Stamp `qty` at live-fill materialization

**Files:**
- Modify: `src/swing_screener/pipeline/reconcile.py:95-121` (`_materialize_fills`), `:135-168` (`_materialized_trade`)
- Test: extend `tests/pipeline/test_reconcile_live.py`

**Step 1: Failing test** (copy the file's existing FakeBroker/fixture idiom — it scripts fills via `submitted_live` logs):

```python
def test_materialized_live_trade_carries_qty(...):
    # arrange a submitted_live log with shares=3 and a broker order filled_qty=3
    ...
    reconcile_live(session, broker, today=date(2026, 7, 20))
    pt = session.scalars(select(PaperTrade).where(PaperTrade.account == "live")).one()
    assert pt.qty == 3

def test_partial_fill_stamps_filled_qty_not_ticket_shares(...):
    # shares=3 on the log, broker filled_qty=1 -> qty must be 1 (venue truth wins)
    ...
    assert pt.qty == 1
```

**Step 2:** Run → FAIL (`qty` is None).

**Step 3: Implement.** Thread the order into the builder: in `_materialize_fills`, pass `qty=int(order.filled_qty)`; in `_materialized_trade`, add the `qty: int | None` keyword and set `qty=qty` on the `PaperTrade`. Venue truth (`filled_qty`) wins over `log.shares`; fall back to `log.shares` only if `filled_qty` is somehow 0/None on a filled order (defensive). Update the Phase-4 partial-fill scope note at `reconcile.py:96-100` — qty is now tracked; the residual-shares limitation stands.

**Step 4:** PASS. **Step 5:** `python -m pytest tests/pipeline/test_reconcile_live.py -v` → commit `feat: stamp broker filled qty on materialized live trades`.

### Task 3: Repo helpers — guardrails state machine + breaker queries

**Files:**
- Create: `src/swing_screener/db/guardrails_repo.py` (keep `repo.py` from growing; same conventions)
- Test: `tests/db/test_guardrails_repo.py`

**Step 1: Failing tests.** Cover, at minimum:

```python
def test_load_creates_default_row_once(session): ...          # get-or-create, id stable
def test_trip_elects_exactly_one_owner(session): ...          # two trip() calls, second returns False
def test_clear_requires_matching_trip_id(session): ...        # stale trip_id -> False, still tripped
def test_halt_then_trip_overwrites_halted(session): ...
def test_edit_never_touches_state_columns(session): ...       # edit while tripped stays tripped
def test_every_transition_appends_event(session): ...
def test_realized_usd_on_sums_closed_live_trades_with_qty(session): ...   # NULL qty skipped
def test_live_drawdown_from_anchor(session): ...              # HWM math over ExitEvent order
def test_live_loss_streak_ordered_by_exit_event_id(session): ...
def test_trades_today_counts_counting_statuses_only(session): ...
def test_mandate_ok_requires_three_breakers_set(session): ...
```

**Step 2:** Run → FAIL (module missing).

**Step 3: Implement** (complete module skeleton — fill query bodies per the design):

```python
"""Guardrails state machine + breaker queries. Every state change is an ATOMIC
conditional UPDATE (rows-affected election) plus one appended AgentGuardrailEvent;
every read on the hot dispatch path is a COLUMN select (never a cached ORM entity,
which the dispatch loop's long-lived Session would serve stale)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from swing_screener.db.models import (
    AgentGuardrailEvent, AgentGuardrails, ExecutionLog, ExitEvent, PaperTrade,
)
from swing_screener.db.repo import _LIMIT_COUNTING_STATUSES

LIVE_ACCOUNT = "live"


@dataclass(frozen=True)
class GuardrailsState:
    """A point-in-time column snapshot of the single agent_guardrails row."""
    state: str
    max_daily_loss_usd: float | None
    max_trades_per_day: int | None
    max_drawdown_usd: float | None
    loss_streak_halt: int | None
    hwm_anchor_date: date | None
    hwm_baseline_usd: float
    trip_id: int | None
    trip_reason: str | None
    sweep_state: str | None


def load_guardrails(session: Session) -> GuardrailsState:
    """Column-select the single row (get-or-create the default released row)."""
    ...


def record_event(session, *, kind, source, breaker="", reason="", values_json="{}") -> int:
    """Append one event row; returns its id. Commits."""
    ...


def trip(session, *, breaker: str, reason: str, source: str) -> int | None:
    """Atomic trip election. Appends the 'trip' event FIRST, then
    UPDATE agent_guardrails SET state='tripped', trip_id=:eid, trip_reason=:r,
    sweep_state='pending', updated_at=:now WHERE state != 'tripped'.
    rows-affected == 1 -> return the event id (caller owns sweep + email);
    0 -> roll the event back? NO: keep the event (it is true that the breaker
    breached) but return None (someone else owns the response)."""
    ...


def clear(session, *, acknowledged_trip_id: int, source: str) -> bool:
    """UPDATE ... SET state='ok', trip_id=NULL, trip_reason=NULL, sweep_state=NULL
    WHERE trip_id=:ack AND state='tripped'. Also clears a plain HALT when
    acknowledged_trip_id is None-sentinel? NO -- halts clear via clear_halt()."""
    ...


def halt(session, *, source: str, reason: str = "manual HALT") -> bool:
    """UPDATE ... SET state='halted' WHERE state = 'ok' (a trip is never downgraded)."""
    ...


def clear_halt(session, *, source: str) -> bool:
    """UPDATE ... SET state='ok' WHERE state = 'halted'."""
    ...


def edit_limits(session, *, source: str, **limits) -> None:
    """SET only the passed breaker/anchor columns + updated_at; never state columns.
    Appends one 'edit' event carrying old->new values_json."""
    ...


def record_sweep_outcome(session, *, trip_id: int, outcome: str, detail: str, source: str) -> None:
    """SET sweep_state=:outcome WHERE trip_id=:trip_id; append a 'sweep' event."""
    ...


def trades_today(session, *, run_date: date) -> int:
    """Counting-status live ExecutionLog rows for run_date (the trades/day input)."""
    ...


def realized_usd_on(session, *, run_date: date) -> float:
    """Sum (exit_price-entry_price)*qty over CLOSED live trades exiting run_date;
    rows with NULL qty contribute 0 (never guessed)."""
    ...


def live_drawdown_usd(session, *, anchor_date: date | None, baseline_usd: float) -> float:
    """Current drawdown from the high-water mark of cumulative realized live $ since
    the anchor, ordered by ExitEvent.id (account == "live", == not .is_()). >= 0."""
    ...


def live_loss_streak(session) -> int:
    """Consecutive realized_r < 0 from the newest live ExitEvent backwards; NULL
    trade_id / realized_r rows skipped (the orphan hazard)."""
    ...


def guardrails_mandate_ok(session) -> tuple[bool, str]:
    """Real-money mandate: max_daily_loss_usd, max_trades_per_day AND max_drawdown_usd
    all set, and state == 'ok'. Names the first failing item (mirrors
    real_money_limits_ok's shape)."""
    ...
```

**Step 4:** PASS all. **Step 5:** `ruff check . && python -m pytest tests/db -v` → commit `feat: guardrails state machine + breaker queries`.

---

## Phase 2 — Enforcement

### Task 4: The brake inside `LiveAdapter.submit` + the real-money mandate

**Files:**
- Modify: `src/swing_screener/pipeline/execution.py` — new step between step 0 (idempotency guard, line ~460) and step 1 (real-money guard, line ~468); mandate addition inside the `is_real_money()` block after `real_money_limits_ok` (~line 482)
- Test: `tests/pipeline/test_execution_guardrails.py` (fixtures copied from `tests/pipeline/test_execution_live.py` — FakeBroker, injected `settings`/`gate_ready_fn`)

**Step 1: Failing tests:**

```python
def test_halted_state_refuses_before_venue(...):        # state='halted' -> skipped
    # broker.submit_order must NOT be called; ExecutionLog status='skipped',
    # detail startswith 'guardrail:'
def test_tripped_state_refuses_before_venue(...): ...
def test_trades_per_day_breaker_blocks_at_cap(...): ...  # 2 counting rows, cap 2 -> skipped
def test_daily_loss_usd_breaker(...): ...                # closed live trade -60$, cap 50 -> skipped
def test_drawdown_breaker(...): ...
def test_loss_streak_breaker(...): ...
def test_brake_enforced_on_paper_host_too(...):          # is_real_money False, HALT still blocks
def test_unset_mandatory_breaker_refuses_real_money(...): # rejected_live naming the breaker
def test_paper_host_exempt_from_mandate(...):            # unset breakers, paper host -> order goes out
def test_guardrail_skip_is_non_counting_and_upgradeable(...): # later submit upgrades in place
```

**Step 2:** FAIL. **Step 3: Implement.** In `LiveAdapter.submit`, after the idempotency guard:

```python
        # 0.5 THE GUARDRAILS BRAKE -- unconditional (paper host included, so the drill
        #     rehearses every trip path), BEFORE the real-money guard and the venue.
        #     A brake refusal is a clamp: log 'skipped' with a 'guardrail: ...' detail
        #     (non-counting, upgradeable -- never burns the key). The dispatch loop owns
        #     the trip RESPONSE (sweep/email); submit only refuses.
        from swing_screener.db import guardrails_repo as gr
        g = gr.load_guardrails(session)
        brake_reason = _guardrail_block(session, g, run_date=run_date)
        if brake_reason is not None:
            self._log(session, intent, run_date=run_date, key=key,
                      status="skipped", detail=f"guardrail: {brake_reason}")
            return OrderResult(status="skipped", account=LIVE_ACCOUNT,
                               detail=f"guardrail: {brake_reason}")
```

with a module-level pure decider mirroring `_limit_block`:

```python
def _guardrail_block(session, g, *, run_date) -> str | None:
    if g.state != "ok":
        return f"brake engaged ({g.state}{': ' + g.trip_reason if g.trip_reason else ''})"
    if g.max_trades_per_day is not None:
        n = gr.trades_today(session, run_date=run_date)
        if n >= g.max_trades_per_day:
            return f"max trades/day: {n} >= {g.max_trades_per_day}"
    if g.max_daily_loss_usd is not None:
        d = gr.realized_usd_on(session, run_date=run_date)
        if d <= -g.max_daily_loss_usd:
            return f"max daily loss: ${d:.2f} <= -${g.max_daily_loss_usd:.2f}"
    if g.max_drawdown_usd is not None:
        dd = gr.live_drawdown_usd(session, anchor_date=g.hwm_anchor_date,
                                  baseline_usd=g.hwm_baseline_usd)
        if dd >= g.max_drawdown_usd:
            return f"max drawdown: ${dd:.2f} >= ${g.max_drawdown_usd:.2f}"
    if g.loss_streak_halt is not None:
        s = gr.live_loss_streak(session)
        if s >= g.loss_streak_halt:
            return f"loss streak: {s} >= {g.loss_streak_halt}"
    return None
```

(Move the import to module top — shown inline above only for anchor clarity.) Then, inside the `is_real_money()` block after the `real_money_limits_ok` check:

```python
            ok3, reason3 = gr.guardrails_mandate_ok(session)
            if not ok3:
                self._log(session, intent, run_date=run_date, key=key,
                          status="rejected_live", detail=reason3)
                return OrderResult(status="rejected", account=LIVE_ACCOUNT, detail=reason3)
```

Update the class docstring's numbered safety list (0.5 brake; mandate as lock-adjacent step 1b).

**Step 4:** PASS. **Step 5:** Full `python -m pytest tests/pipeline -v` (existing live tests must stay green — default guardrails row is released/unset, so behavior is unchanged until configured) → commit `feat: guardrails brake + real-money mandate inside LiveAdapter.submit`.

### Task 5 (Stage-1): qty ≤ 0 never reaches the venue

**Files:**
- Modify: `src/swing_screener/notify/run.py` dispatch loop (~line 711) — filter before submit
- Modify: `src/swing_screener/pipeline/execution.py` `LiveAdapter.submit` — defensive refusal
- Test: extend `tests/pipeline/test_execution_guardrails.py` + `tests/notify/test_run_execution.py` (find the existing dispatch-loop test file by grepping `collected_intents` under `tests/notify/`)

**Steps (TDD as above):** (1) test that a `shares=0` intent produces a `skipped` ticket line with detail `"unsized (0 shares)"` and the adapter/broker is never called; (2) test the LiveAdapter defensive path directly. Implementation: in the dispatch loop, before `_execution_halted`, `if intent.shares <= 0: tickets[...] = OrderTicketLine(..., status="skipped", detail="unsized (0 shares)"); continue`. In `LiveAdapter.submit` step 2.5: refuse `shares <= 0` with a logged `skipped` row (never a venue 422). Commit `fix: zero-share intents skip cleanly instead of burning venue rejects`.

### Task 6: Trip protocol module + dispatch-loop response

**Files:**
- Create: `src/swing_screener/pipeline/guardrails.py`
- Modify: `src/swing_screener/notify/run.py` (~707-733): consult guardrails beside `_execution_halted`
- Test: `tests/pipeline/test_guardrails_trip.py`, extend the notify dispatch tests

**Step 1: Failing tests:**

```python
def test_evaluate_and_trip_persists_before_sweep(...):   # broker raises -> state still tripped, sweep_state='partial'
def test_trip_owner_runs_sweep_once(...):                # second evaluate on tripped row: no second sweep/email
def test_sweep_writes_disarm_event_with_guardrail_reason(...):
def test_pending_or_partial_sweep_rerun_next_cycle(...):
def test_dispatch_loop_halts_batch_on_trip(...):         # 3 intents, breaker trips after 1st -> 2 never submitted
def test_halted_state_stops_dispatch_and_pulls_entries(...):
```

**Step 2:** FAIL. **Step 3: Implement** `pipeline/guardrails.py`:

```python
"""The trip protocol: evaluate breakers, and on a trip run the ORDERED response --
(1) persist the tripped state + event FIRST (the brake holds no matter what follows),
(2) the disarm sweep (entry pulls + stop protection, key_suffix from the TRIP id so
cross-process re-runs collapse to the same client_order_ids), (3) record the sweep
outcome, (4) the alert email (EmailLog-deduped, isolated try/except). Also the
re-run owner: while sweep_state is 'pending'/'partial', every caller re-runs the
sweep (ensure_stop_protection skips already-protected symbols, so re-runs are safe)."""


def evaluate_breakers(session, *, run_date) -> tuple[str, str] | None:
    """(breaker, reason) for the first breached breaker, else None. Pure read;
    reuses execution._guardrail_block's per-breaker queries but WITHOUT the
    state!='ok' short-circuit (state is the outcome here, not an input)."""


def respond_to_trip(session, *, breaker, reason, source, broker, emailer) -> None:
    """The full protocol. trip() election decides ownership; a None election result
    means another process owns the response -- return quietly. broker may be None
    (evening-screen secrets gap): persist with sweep_state='pending' and skip the
    sweep -- the next cycle owns it. emailer is a zero-arg-composable callable seam
    (tests inject a spy; prod wires Task 10's sender)."""


def resume_incomplete_sweep(session, *, broker, source) -> bool:
    """If state=='tripped' and sweep_state in ('pending','partial'), re-run the sweep
    + record the outcome. Called from every dispatch/screen cycle."""
```

The sweep body mirrors the kill-switch block at `notify/run.py:715-729` (`pull_entry_orders` + `ensure_stop_protection(key_suffix=f"guardrail-{trip_id}")`) and writes `DisarmEvent(reason=f"guardrail:{breaker}", orders_cancelled=len(entries))` via a `_record_disarm`-style best-effort helper. In the dispatch loop, extend the per-intent check:

```python
                    if _execution_halted(adapter, _mode_reader):
                        ...existing kill-switch block...
                        break
                    if isinstance(adapter, LiveAdapter):
                        gpipe.resume_incomplete_sweep(session, broker=live_broker, source="digest")
                        breach = gpipe.evaluate_breakers(session, run_date=run_date)
                        g = gr.load_guardrails(session)
                        if breach is not None or g.state != "ok":
                            if breach is not None:
                                gpipe.respond_to_trip(session, breaker=breach[0],
                                    reason=breach[1], source="digest",
                                    broker=live_broker, emailer=_trip_emailer(...))
                            log.warning("guardrails brake: halting dispatch for %s %s",
                                        kind, run_date)
                            break
```

(`_trip_emailer` lands in Task 10 — until then pass `None` and have `respond_to_trip` treat None as "skip step 4".) The submit-side brake (Task 4) stays the hard backstop; this loop check is the response trigger.

**Step 4:** PASS. **Step 5:** full pipeline + notify suites → commit `feat: guardrail trip protocol + dispatch-loop response`.

### Task 7: Evening-screen evaluation + dispatch-time reconcile freshness

**Files:**
- Modify: `src/swing_screener/pipeline/run.py` (~584-587, right after the `reconcile_live` call)
- Modify: `src/swing_screener/notify/run.py` (dispatch setup, ~445-448): when `exec_mode=="live"` and `live_broker` is not None, run a `reconcile_live` pass before building/dispatching intents
- Test: extend `tests/pipeline/test_run_live_reconcile.py` + notify dispatch tests

**Failing tests:** (1) evening screen with a same-day losing close beyond the cap → guardrails row tripped, `sweep_state` set (`'pending'` when broker is None); (2) morning dispatch reconciles a filled-then-stopped-out order before evaluating breakers → daily-loss trip blocks all submits that morning. **Implementation:** in `run.py`, after `reconcile_live(...)`: `breach = evaluate_breakers(...); if breach: respond_to_trip(..., source="screen", broker=broker, emailer=...)` plus `resume_incomplete_sweep`. In `notify/run.py`: `reconcile_live(session, live_broker, today=run_date)` wrapped in the same swallow-everything posture as the dispatch loop (a reconcile failure must never block the digest). Commit `feat: breaker evaluation at evening reconcile + dispatch-time live refresh`.

### Task 8 (Stage-1): Play-type execution scoping

**Files:**
- Modify: `src/swing_screener/settings.py` (Settings field + `load_settings`), `src/swing_screener/notify/run.py` (~709: filter `collected_intents`)
- Test: `tests/notify/test_run_execution_scope.py`, `tests/test_settings_broker.py` pattern for the env parse

**Failing tests:** `SWING_EXECUTE_PLAY_TYPES="reversal"` → continuation intents get `skipped` tickets (`detail="play type not in execution scope"`), reversal intents dispatch; unset → both dispatch (today's behavior); garbage value → fail-safe to empty set (NOTHING dispatches, loud warning — fail-closed, matching the mode-coercion posture). **Implementation:** `execute_play_types: frozenset[str] | None` parsed like `deep_analysis_kinds` (`None` when unset = allow-all; empty-after-validation = allow-none + warning; validate members against `{"continuation", "reversal"}`). Filter in the dispatch loop before the submit (synthetic skipped ticket so the digest stays honest). Surface the knob in `/api/config`'s execution section (`_cfg_row("execute play types", "SWING_EXECUTE_PLAY_TYPES", ...)`). **Seam for the Strategy Board (Task 22):** route the decision through one function — `effective_execution_scope(settings, session) -> frozenset[str]` (for now: just the env parse; `session` accepted and unused) — so the cockpit `disabled_play_types` subtraction lands there without touching the loop again. Commit `feat: SWING_EXECUTE_PLAY_TYPES execution scoping`.

### Task 9 (Stage-1): Orphan adoption on duplicate client_order_id

**Files:**
- Modify: `src/swing_screener/pipeline/execution.py` (`LiveAdapter.submit` exception path, ~508-518), `src/swing_screener/pipeline/broker.py` + `broker_alpaca.py` (add `get_order_by_client_id(client_order_id) -> BrokerOrder | None` to the protocol + Alpaca impl `GET /v2/orders:by_client_order_id`; FakeBroker in tests gains it)
- Test: extend `tests/pipeline/test_execution_live.py` + `tests/pipeline/test_broker_alpaca.py` (MockTransport pattern)

**Failing test:** broker raises a duplicate-client_order_id `HTTPStatusError`; adapter fetches the existing order and logs `submitted_live` with the real `broker_order_id` (upgradeable path) instead of `rejected_live`. **Implementation:** in the except block, before giving up: `existing = self._broker.get_order_by_client_id(key)` (guarded — any failure falls through to the current `rejected_live` path); if found and status not in `("canceled", "rejected")`, log `submitted_live` + return. Protocol addition is backward-safe (all impls in-repo). Commit `fix: adopt orphaned venue orders on duplicate client_order_id`.

**Phase 2 boundary:** `python -m pytest tests -x -q`, `ruff check .`, `MYPYPATH=src mypy src/swing_screener`.

---

## Phase 3 — Alerts + surfacing

### Task 10: Trip + rejection alert emails

**Files:**
- Modify: `src/swing_screener/notify/alerts.py` (compose), `src/swing_screener/notify/run.py` (wire `_trip_emailer` into the Task-6 seam; emit `rejected_live`/`canceled` alerts), `src/swing_screener/pipeline/run.py` (minimal sender for evening trips: `resolve_sender()` + `get_secret("DIGEST_TO")`, isolated try/except)
- Test: `tests/notify/test_guardrail_alerts.py`

**Failing tests:** (1) a trip sends exactly one email (send-then-log, `EmailLog(kind="guardrail", alert_key=str(trip_event_id))`), re-run no-ops; (2) send failure leaves no EmailLog row (retried next cycle) and never aborts the sweep; (3) a reconcile pass that flips a log to `rejected_live`/`canceled` emits one `EmailLog(kind="execution", alert_key=sha1(sorted log ids))` alert email listing the rows (clone `_exit_alert_key`/`_exit_already_sent` idiom at `notify/run.py:118-186`). **Implementation:** compose functions in `alerts.py` mirroring `compose_exit_alert`; emitters mirroring `_emit_pending_exit_alert` exactly (SEND then LOG, IntegrityError catch). Evening screen: build the sender lazily inside the trip path only (no transport at import). Commit `feat: guardrail trip + live-rejection alert emails`.

### Task 11 (Stage-1): Hourly live reconcile

**Files:**
- Modify: `src/swing_screener/pipeline/exitcheck.py` (`run_exit_check`, ~116-180)
- Test: extend `tests/pipeline/test_exitcheck.py` (or create `test_exitcheck_live.py` if none — check `tests/pipeline/` first)

**Failing test:** with `mode==live` (or open live exposure) and a broker buildable, the hourly run materializes a fill / reconciles an exit; broker-build failure degrades to a warning (screen-run posture at `pipeline/run.py:578-583`); no broker + no exposure = no-op. **Implementation:** copy the guarded broker-build + `reconcile_live` block from `run.py:570-587` into `run_exit_check` (extract a tiny shared helper `pipeline/live_sync.py:maybe_reconcile_live(session, *, today) -> int` and call it from both sites — DRY). **Plus (Task-6 red-team):** the same hourly pass calls `guardrails.resume_incomplete_sweep` (broker built on demand, independent of exec mode) so a crashed trip sweep is retried within the hour, not at the next digest. **Plus (Task-10 handoff):** the hourly job's rejection alerts should be true at-least-once — query un-alerted `rejected_live`/`canceled` rows (join against `EmailLog kind='execution'` via the emitter's id-set key) instead of cloning the digest's before/after diff, whose failed sends are never retried; `_emit_live_rejection_alert`'s dedup already supports it. Also call `resume_incomplete_sweep` mirroring the digest hoist (state-check before broker build). **Plus (Task-7 review):** (a) move `broker_error_detail` out of `preflight.py` into `pipeline/broker.py` (7-line pure function; severs the guardrails→preflight edge, dissolving the replay↔run import cycle so both call sites can import `pipeline.guardrails` at module level — update the cockpit sharer + drop the lazy import in `run.py`); (b) extract the now-thrice-repeated resume→load→evaluate→respond consult into `gpipe.consult(session, *, run_date, source, broker, emailer)` (Task 7's reviewer: wait until this third site exists — it now does). Note: Task 7's guardrails-consult tests live in `tests/pipeline/test_run_guardrails.py` (new file), not `test_run_live_reconcile.py` as originally written here. This also makes the Task-10 rejection alerts fire same-hour. Commit `feat: hourly live reconcile in the intraday exit job`.

### Task 12: Preflight + safety report + masthead surfacing

**Files:**
- Modify: `src/swing_screener/pipeline/preflight.py` (new check beside `_check_caps`), `src/swing_screener/cockpit/routers/safety.py` (`execution_safety`: `guardrails_mandate` + brake state; `gate`: `brake_state` chip field)
- Test: extend `tests/pipeline/test_preflight.py`, `tests/cockpit/test_safety_router.py`

**Failing tests:** preflight shows `guardrails` check — critical & failing when a mandatory breaker is unset or state != 'ok' with a real-money host, advisory-pass on paper host; `/api/execution/safety` payload gains `"guardrails": {"ok":…, "reason":…, "state":…}`; `/api/gate` gains `"brake_state": "ok"|"halted"|"tripped"`. **Implementation:** straightforward; reuse `guardrails_mandate_ok` + `load_guardrails`. Commit `feat: guardrails in preflight, safety report, gate poll`.

### Task 13 (Stage-1): Scoreboard live dollars

**Files:**
- Modify: `src/swing_screener/cockpit/scoreboard.py:220-234` (live card) + `:257-276` (combined)
- Test: extend `tests/cockpit/test_scoreboard.py`

**Failing test:** closed live trades with `qty` produce `realized_usd = Σ (exit−entry)×qty` on the live card and inside `combined`; NULL-qty legacy rows contribute 0 with an honesty note (`"n_unsized"` count on the card). **Implementation:** replace the hardcoded `realized_usd=None` with `guardrails_repo.realized_usd_on`-style aggregation (all-time, not per-day — add `live_realized_usd_total(session)` to `guardrails_repo`). Commit `feat: live book dollar P&L on the metrics scoreboard`.

### Task 14: Auditor classification

**Files:**
- Modify: `src/swing_screener/journal/audit_compliance.py` (grade guardrail activity), `src/swing_screener/journal/audit_run.py` (breach rules)
- Test: extend `tests/journal/test_audit_compliance.py` / `test_audit_anomaly.py` siblings

**Failing tests:** (expected) a `DisarmEvent(reason='guardrail:…')` + matching trip event grades info, not breach; kill-switch/halt DisarmEvents (reasons `kill-switch`/`halt`, added in Task 6) also grade expected; (breach) counting live submit on a day whose guardrails state was tripped; trip with no `EmailLog(kind='guardrail')` — EXCEPT trips already cleared (a cockpit clear implies operator awareness; Task 10's emitter deliberately never mails cleared trips — detect via a later `kind='clear'` guardrail event); `sweep_state='partial'` persisting a full day. Day-granularity only. **Implementation:** follow the `n_clamps` precedent (`audit_compliance.py:32-33`) and the `disarm:{day}` breach key idiom (`audit_run.py:175-195`) with new keys `guardrail-trip:{day}` etc. Commit `feat: auditor grades guardrail conduct`.

**Phase 3 boundary:** full suite + ruff + mypy.

---

## Phase 4 — Cockpit

### Task 15: `GET/POST /api/guardrails`

**Files:**
- Modify: `src/swing_screener/cockpit/routers/safety.py` (same router; endpoints beside `/api/disarm`)
- Test: `tests/cockpit/test_guardrails_router.py` (copy `test_safety_router.py`'s app/client fixtures)

**Failing tests:**

```python
def test_get_returns_state_and_history(...): ...
def test_post_edit_limits_appends_event(...): ...
def test_post_halt_runs_sweep_under_disarm_lock(...):     # 409 when lock held
def test_post_clear_requires_ack_trip_id(...):            # stale id -> 409 state error
def test_primary_write_failure_is_503_never_200(...):     # G7-style perm error must surface
def test_dry_run_halt_previews_sweep(...):
```

**Implementation:** `GET /api/guardrails` (DB-only: state + limits + last N events — never rides the broker-calling safety poll). `POST /api/guardrails` with an `action` body: `edit` | `halt` | `clear_halt` | `clear_trip` (+`ack_trip_id`), `dry_run` query for `halt`. **Trip-aware disarm (Task-6 red-team requirement):** `/api/disarm` must check `load_guardrails` first — when state is `tripped` with `sweep_state` pending/partial, route through `resume_incomplete_sweep` instead of the raw sweep, so both processes use the same `guardrail-{trip_id}` client_order_ids and the venue's duplicate-ID rejection collapses the concurrent-sweep race (duplicate-GTC-stop hazard on margin accounts); this also finally writes `sweep_state='complete'` when the cockpit finishes a tripped book's cleanup (otherwise the TRIPPED — SWEEP PARTIAL banner sticks forever). Conventions: `Depends(_require_cockpit)`; `halt`/`clear_trip` acquire `disarm_lock` non-blocking (409 on conflict) because halt runs the sweep via the broker factory; snapshot invalidate + `action_nonce.bump()` in `finally` on real venue-touching runs; **the state write lets `SQLAlchemyError` propagate** (app-level 503) — only the event append may use the `_record_disarm` best-effort posture. Commit `feat: /api/guardrails endpoints`.

### Task 16: SafetyScreen guardrails panel

**Files:**
- Modify: `cockpit-ui/src/screens/SafetyScreen.tsx` (new panel between ARMING LOCKS and HARD CAPS, ~line 155-255), `cockpit-ui/src/components/` (new `GuardrailsPanel.tsx`), masthead brake chip (`Masthead.tsx`, beside the execution-mode chip ~line 169)
- Reference idioms: `DisarmControl.tsx` (HoldToConfirm composition — `armed` is an async wait-gate; gate trip-clear with `disabled={!acknowledged}` instead), `CloseTradeForm.tsx` (form structure + `ApiError.fieldErrors`), `usePolling` paramsKey

**Steps:** (1) `GuardrailsPanel`: state banner (`OK`/`HALTED`/`TRIPPED — <reason>` + `SWEEP PARTIAL` loudly), limits form (edit action), HALT button (900 ms hold + dry-run preview on hold-start), trip-clear (ack checkbox → enabled 900 ms hold), event history list. Caption: *"a brake, not a knob — env stays the master arm."* (2) Masthead chip renders `brake_state` from the existing `/api/gate` poll (no new poll). (3) **Verification (no test framework):** CSS brace-balance count on every stylesheet touched; `vite dev` + Playwright MCP screenshots of: released, halted, tripped-partial, clear flow. (4) `npm run build` (pin CRLF: check `.gitattributes` eol=lf on generated static). Commit `feat: guardrails panel on the Safety screen`.

### Task 17: Glossary

**Files:** `cockpit-ui` glossary source `glossary.data.json` (+ `HelpTerm` usages on the new panel), pytest citation-guard will enforce coverage.

Add terms: `guardrail`, `HALT (brake)`, `trip`, `drawdown anchor / high-water mark`, `loss streak`, `sweep (guardrail)` — each with source-links to `pipeline/guardrails.py` / the design doc. Run the citation-guard test. Commit `docs: glossary entries for guardrails`.

---

## Phase 4b — Strategy Board (addendum, approved 2026-07-18: tighten-only selection)

### Task 22: `disabled_play_types` — the cockpit scope subtraction

**Files:**
- Modify: `src/swing_screener/db/models.py` (`AgentGuardrails.disabled_play_types: Mapped[str] = mapped_column(String(64), default="", server_default="")` — comma-separated, bounded), new Alembic revision (down_revision = current head) adding the column
- Modify: `src/swing_screener/db/guardrails_repo.py`: `GuardrailsState.disabled_play_types: frozenset[str]` (parsed), `set_disabled_play_types(session, *, disabled: set[str], source: str)` — validates ⊆ `{"continuation", "reversal"}`, MIN(id)-pinned plain UPDATE touching ONLY this column + updated_at, one 'edit' event (breaker `'disabled_play_types'`, old→new values_json), single commit
- Modify: wherever Task 8 put `effective_execution_scope(settings, session)`: effective = env ceiling − `load_guardrails(session).disabled_play_types`
- Test: extend `tests/db/test_guardrails_repo.py` + the Task 8 scope tests (disabled set subtracts; re-enable returns to ceiling, never past it; unknown play type → ValueError)

TDD as usual; `alembic heads` single; commit `feat: cockpit play-type subtraction (tighten-only strategy scope)`.

### Task 23: `GET /api/strategies` — the evidence ranking

**Files:**
- Create: `src/swing_screener/cockpit/routers/strategies.py` (register like the other routers)
- Test: `tests/cockpit/test_strategies_router.py`

Per play type return: `in_ceiling` (env), `disabled` (cockpit), `effective` (traded now), `playbook_present` (edge/<pt>.md + verdicts exist), `tier` (best verdict tier in the sidecar: forward_confirmed > replay_screened > hunch), `best_cohort` {dimension, ci_low, expectancy, n} (highest ci_low among the best tier's entries), `calibration` (reuse the autonomy gate's counts — scored-high/scored-low/tickers vs the 20/20/8 floors; do NOT reimplement the arithmetic, import from `pipeline.autonomy`), `gate_ready`, and forward shadow stats (would_surface facet: n, mean R over the trailing window — reuse the existing stats helpers; if no clean helper exists, expose what `edge/<pt>.md` frontmatter carries and note the gap rather than inventing math). Rank: tier desc, then ci_low desc. DB/file-only — no broker call. Commit `feat: /api/strategies evidence ranking`.

### Task 24: STRATEGY SCOPE panel + docs

**Files:**
- Modify: `cockpit-ui/src/screens/SafetyScreen.tsx` (+ new `StrategyBoard.tsx` component below the guardrails panel), glossary terms (evidence tier, CI floor, execution scope/ceiling), docs (`using-meridian.md` Safety section)

Per-strategy row: rank, IN SCOPE/OUT lamp, tier chip, ci-low + n, calibration countdown, gate lamp; disable/enable toggle = 400 ms hold (light-decision tier — no venue state moves; enable is capped at the env ceiling and the control states that). Caption: *"selection is evidence-gated: the ceiling changes via env ceremony; the board can only subtract or restore within it."* Same UI verification ritual (brace balance, vite dev + Playwright states). Commit `feat: strategy board panel`.

---

## Phase 5 — Infra, docs, self-healing stops

### Task 18 (Stage-1): Infra broker wiring

**Files:**
- Modify: `infra/modules/jobs.bicep` (~89-104 params, ~217-229 env): add `broker` (`SWING_BROKER`) and `allowRealMoney` (`SWING_BROKER_ALLOW_REAL_MONEY`) params, template-default `''` (fail-safe off), mirroring `executionMode`; `infra/main.bicepparam` (~54-80): placeholders commented, NOT set
- Note in-file: Alpaca secrets need NO bicep change — `get_secret`'s Key Vault fallback resolves `swing-alpaca-key` / `swing-alpaca-secret` / `swing-alpaca-host` once Oliver creates them (`config_secrets.py:74-87`); `KEY_VAULT_URL` is already on every job.

No unit test — verification is `az bicep build` (lint) locally. Document in the PR that CD never applies bicep: the arming ceremony includes `az deployment sub create … seedSecrets=false`. Commit `infra: SWING_BROKER + allow-real-money params (template default: disarmed)`.

### Task 19: Self-healing stop protection (the day-TIF answer)

**Files:**
- Modify: `src/swing_screener/pipeline/run.py` — after the live reconcile block: when a broker is in hand and open live exposure exists, run `ensure_stop_protection(broker, latest_recorded_stop-lookup, key_suffix=f"screen-{today:%Y%m%d}")` and log restored/unprotected loudly
- Test: extend `tests/pipeline/test_run_live_reconcile.py`

**Failing test:** an open live position with no venue stop gets a GTC stop re-submitted during the evening screen; an already-protected one is untouched (idempotent). **Rationale (from the audit):** bracket children inherit the parent's hardcoded `day` TIF and may die at the close; rather than guessing Alpaca's behavior, make every evening screen re-assert the protection invariant — the Stage-0 drill then empirically answers the TIF question, and either way no swing hold sits unprotected overnight for more than one session. If the drill confirms legs die EOD, a follow-up may also flip the entry bracket to `gtc`; that is deliberately NOT in this plan (entry-TIF semantics are a strategy decision). Commit `feat: evening screen re-asserts venue stop protection on the live book`.

### Task 20: Docs + runbook

**Files:**
- Modify: `docs/runbooks/arming-alpaca-live.md`: guardrails section (mandatory breakers in the arming checklist, the HALT/trip/clear operator flow — including the Task-6 behavior note: while a trip's sweep is unfinished, every digest/hourly cycle retries it even with `SWING_EXECUTION_MODE=off`, so mode-off no longer guarantees zero venue writes until the sweep completes; the sweep only ever reduces exposure), the small-account env block (sizing envs sized to actual funded equity — values are Oliver's call, the runbook documents *which* envs), an Azure appendix (per-job `az containerapp job update --set-env-vars` flips; never `job start --env-vars` — it replaces template env and drops secret refs)
- Modify: `docs/using-meridian.md` (Safety screen section: the brake), `docs/cockpit.md` if present-pattern requires
- Modify: `docs/plans/2026-07-18-agent-guardrails-design.md`: status line → implemented, link PR

Commit `docs: guardrails operator flow + Azure arming appendix`.

### Task 21: Final verification

1. `python -m pytest tests -q` — zero failures.
2. `ruff check .` — clean.
3. `MYPYPATH=src mypy src/swing_screener` — clean (worktree-correct invocation).
4. `alembic heads` — exactly one head.
5. cockpit-ui: brace-balance count on changed CSS; `npm run build`; Playwright MCP walkthrough of the four panel states.
6. REQUIRED SUB-SKILL before claiming done: superpowers:verification-before-completion.
7. Then the Stage-0 drill (runbook steps, Alpaca paper host — operator-driven, out of code scope): preflight GO → forced HALT mid-dispatch (≤1-order leak bound) → forced drawdown trip with a tight limit → verify sweep + email + TRIPPED banner + clear flow → check whether bracket legs survive the entry day's close (the TIF question) with Task 19's nightly re-assert as the net.

---

## Explicitly out of scope (per design)

Unrealized-P&L breakers; cockpit arming/mode flips/sizing writes; per-job guardrail divergence; entry-TIF change (pending drill evidence); Robinhood anything; closed-live-trades listing on the Trades screen (tracked separately as a nice-to-have).

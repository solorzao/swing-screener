# Phase 3 — execution adapters + the autonomy gate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give the Phase-2 `OrderIntent` a pluggable **execution adapter** — a human-placeable order ticket (no money) + a `paper` adapter that opens real *simulated* positions via the existing fill engine — plus the default-off safety scaffolding (kill switch, hard limits, an advisory autonomy gate) the money boundary will need, while it has nothing dangerous to guard.

**Architecture:** A `research | paper | live` **account** dimension on `PaperTrade` isolates the curated intent book from the research grid *by construction* (existing aggregates pin to `research`); the paper adapter just opens an `account="paper"` row and the existing stepper (`advance_open`/`evaluate_exit`) fills + closes it. The adapter is an **injected seam** in `send_digest` (the test suite can never place an order), batch-dispatched in one try/except. A three-state `execution_mode` (default `off`) is the master switch + kill switch; hard limits are clamped *inside* `submit()` off a summed `ExecutionLog`. An advisory `autonomy_gate` (mirrors `propose()`) runs a real calibration test and only ever reports. **Robinhood/live is Phase 4** — zero broker code here.

**Tech Stack:** Python 3.12, SQLAlchemy + Alembic, numpy, pytest. No new deps.

**Governed by:** [North Star](../NORTH_STAR.md) (#1/#3/#4/#6 + kill switch) + the [Phase-3 design](2026-06-21-phase3-execution-adapters-design.md). Builds on Phase 0/1/2 (#41/#43/#45).

---

## Design invariants (hold for every task)
- **Money never auto-moves.** `execution_mode` defaults `off` (= exactly today's behavior); `live` is unbuilt. Levels are copied verbatim from the rules engine — never recomputed or LLM-set.
- **The paper book is isolated by construction.** Every CLOSED-trade research aggregate filters `account="research"`. `advance_open`/`load_open_paper_trades` stay inclusive (they must advance paper trades too).
- **Limits are clamped in code**, inside `submit()`, never trusting the caller (the `_clamp_conviction` template).
- **Two migrations** (T1 `account`, T2 `execution_log`) chain: T2's `down_revision` = T1's revision. Run `alembic heads` after each → exactly one head. Alembic is installed in the venv.
- **Commit discipline:** gate on green (`.venv\Scripts\python.exe -m ruff check .`, `-m mypy`, `-m pytest -q`) BEFORE each commit. No `--no-verify`. Co-Author trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## Task 1: The `account` dimension (isolate the intent book)

**Files:** Modify `db/models.py` (PaperTrade) + an Alembic migration; `db/repo.py` + `analytics/performance.py` + `pipeline/reflect.py` (add the `account="research"` filter to CLOSED-trade reads); Tests across those.

**Step 1 — Model + migration.** Add to `PaperTrade`: `account: Mapped[str] = mapped_column(String(16), default="research", index=True)`. Hand-write `alembic/versions/<rev>_paper_trade_account.py` (`down_revision` = current head via `alembic heads`): `op.add_column("paper_trades", sa.Column("account", sa.String(16), nullable=False, server_default="research"))` + the index; **backfill is automatic via `server_default`** (existing rows → "research"). Verify it applies on a throwaway sqlite + single head.

**Step 2 — Pin research reads.** Find every CLOSED-trade aggregate and add `PaperTrade.account == "research"`:
- `repo.load_closed_paper_trades` (repo.py ~89) and `repo.score_analyst_calls` (repo.py ~122 — the paper-trade join).
- `repo.load_scored_analyst_calls` is keyed on `AnalystCall`, not PaperTrade — leave unless it joins PaperTrade.
- Any leaderboard/summary loader in `analytics/performance.py` / `pipeline/replay.py` that reads closed `PaperTrade`s for the research leaderboard.
- **Do NOT touch** `repo.load_open_paper_trades` (advance_open must keep advancing paper trades) or the shadow-booking path (it writes research, the default).
Grep `PaperTrade` across `src/` and audit each read; document which you filtered and why each other one is exempt.

**Step 3 — TDD.** A closed `account="paper"` trade is excluded from `load_closed_paper_trades`, the leaderboard, and `score_analyst_calls`' attribution, while an identical `account="research"` trade is included; `load_open_paper_trades` returns BOTH (paper trades still get stepped). Existing tests stay green (default `research` preserves all current behavior). Migration applies on fresh sqlite; single head.

**Step 4 — Commit:** `feat(execution): account dimension isolates the intent book from the research grid`.

---

## Task 2: The `ExecutionLog` (idempotency + audit + limit source)

**Files:** `db/models.py` (new `ExecutionLog`) + a migration (`down_revision` = T1's rev); `db/repo.py` (helpers); Tests.

**Step 1 — Model.** `ExecutionLog(Base)` (`execution_logs`): `id`, `created_date: date`, pick keys (`ticker String(16)` idx, `timeframe String(32)`, `play_type String(16)`, `run_date: date`), `account String(16)`, `mode String(16)`, the order spec (`side String(8)`, `limit_price: float`, `shares: int`, `stop: float`, `target: float`, `risk_dollars: float`, `notional: float`), `status String(16)`, `detail String(512)`, and `idempotency_key: String(64)` with a `UniqueConstraint("idempotency_key")` (the `EmailLog` pattern, models.py ~158). Bounded strings (Azure SQL). Migration single-head; applies on sqlite.

**Step 2 — Repo helpers.** `add_execution_log(session, **fields) -> ExecutionLog` (add→commit→refresh; on `IntegrityError` from the unique key → return the existing row, the idempotent no-op). `execution_logs_for_day(session, *, run_date, account) -> list[ExecutionLog]` for the limit sums (only `status` in the "counts against limits" set — filled/recorded, not skipped). `count_open_positions(session, *, account) -> int` (open `PaperTrade`s for the account).

**Step 3 — TDD.** Round-trip; the unique key makes a duplicate `add_execution_log` a no-op returning the first row; `execution_logs_for_day` filters by date+account and excludes `skipped`. Build rows in an in-memory session.

**Step 4 — Commit:** `feat(execution): ExecutionLog store (idempotency + audit + limit source)`.

---

## Task 3: Execution settings + the order spec (pure)

**Files:** `settings.py` (mode + caps + `resolve_execution`); `pipeline/insight.py` (OrderIntent execution fields); Tests.

**Step 1 — Settings.** Add `execution_mode: str` (env `SWING_EXECUTION_MODE`, default `"off"`, validated to `{off, paper, live}` — unknown → `off` with a `log.warning`), `max_daily_notional: float | None` (`SWING_MAX_DAILY_NOTIONAL`), `max_daily_loss: float | None` (`SWING_MAX_DAILY_LOSS`), `max_concurrent: int | None` (`SWING_MAX_CONCURRENT`), reusing the `_opt_float`/`_opt_int` parsers. A frozen `Limits{max_daily_notional, max_daily_loss, max_concurrent}` + a pure `resolve_execution(settings) -> tuple[str, Limits]`. Mirror `resolve_risk_unit` + the `deep_analysis_*` default-off style.

**Step 2 — Order-spec fields.** Add to `OrderIntent` (insight.py): `side: str = "long"`, `limit_price: float`, `order_type: str = "market"`, `time_in_force: str = "day"`. In `build_order_intent`, set `limit_price = facts.entry_ceiling` (the deterministic buy-at-or-below ceiling) — copied, never computed. `notional = shares * limit_price` is derivable (don't store if trivially computed at use). Keep levels verbatim from facts.

**Step 3 — TDD.** `resolve_execution`: default → `("off", Limits(None,None,None))`; each env parsed; unknown mode → off + warning; caps tolerate garbage. `build_order_intent` sets `side`/`limit_price`/defaults from facts (limit_price == entry_ceiling). Pure + deterministic.

**Step 4 — Commit:** `feat(execution): execution_mode + hard-limit settings + order-spec fields`.

---

## Task 4: The adapter interface + `manual` adapter + limits (clamped in code)

**Files:** Create `pipeline/execution.py`; Test `tests/pipeline/test_execution_manual.py`.

**Step 1 — Types.** 
```python
from typing import Protocol
@dataclass(frozen=True)
class OrderResult:
    status: str      # recorded | filled_paper | skipped | rejected
    account: str
    detail: str
    trade_id: int | None = None
    broker_order_id: str | None = None

class ExecutionAdapter(Protocol):
    name: str
    def submit(self, intent, *, session, run_date, limits) -> OrderResult: ...
```
- `idempotency_key(intent, run_date) -> str`: a sha1 of `(ticker, timeframe, play_type, run_date, side)` (the `_exit_alert_key` template, run.py ~80).
- `_limit_block(session, intent, *, run_date, account, limits) -> str | None`: returns a reason string if a limit would be breached, else None. Checks: per-trade `risk_dollars`/`shares` already bounded by sizing; **per-day notional** = sum of today's `execution_logs_for_day` notional + this order's > `limits.max_daily_notional`; **per-day loss** = today's realized losses (from closed `account` trades) below `-max_daily_loss`; **max-concurrent** = `count_open_positions(account) >= max_concurrent`. `None` cap = unbounded for that check. Pure-ish (reads only).
- `NoOpAdapter` (`name="off"`): `submit` returns `OrderResult("skipped", "research", "execution off")` and writes nothing — the default, today's behavior.
- `ManualAdapter` (`name="manual"`): runs `_limit_block` → if blocked, `add_execution_log(status="skipped", detail=reason)` + `OrderResult("skipped", ...)`; else `add_execution_log(status="recorded", ...)` (the order ticket) + `OrderResult("recorded", account="manual"|"research", trade_id=None)`. Opens NO position. Idempotent via the unique key.

**Step 2-4 — TDD:** the ticket is logged with side/limit/shares/stop/target; a duplicate submit is a no-op (one row); each limit blocks at/over its edge and logs `skipped` with the reason; `None` caps don't block; the NoOp adapter writes nothing. In-memory DB; no network.

**Step 5 — Commit:** `feat(execution): adapter interface + manual ticket + in-code hard limits`.

---

## Task 5: The `paper` adapter (opens a real simulated position)

**Files:** `pipeline/execution.py` (`PaperAdapter`); Test `tests/pipeline/test_execution_paper.py`.

**Step 1 — Spec.** `PaperAdapter(name="paper")`: runs `_limit_block` (skip+log if blocked); else opens a **filled** `PaperTrade` from the intent and logs the ticket. Mirror how the shadow book constructs a filled trade (find the open/fill-construction path the shadow booking uses — likely in `pipeline/run.py`/`shadow.py`; reuse it, don't re-derive the fill economics). The row: `account="paper"`, `arm="baseline"`, `variant="default"`, `fill_status="filled"`, `status="open"`, `entry_price = intent.limit_price`, `risk = entry_price - stop` (assert > 0), `stop`/`target` from the intent, `entry_date = opened_date = run_date`, `signal_id` if available. Then `add_execution_log(status="filled_paper", ...)` and `OrderResult("filled_paper", account="paper", trade_id=pt.id)`. **Open trades must carry a concrete `entry_price` + `risk`** (advance_open asserts this, shadow.py:168).

**Step 2-4 — TDD (integration):** submitting an intent opens one `account="paper"` open `PaperTrade` with the right entry/risk/levels + an ExecutionLog `filled_paper` row; then drive `advance_open` with bars that hit the target/stop and assert the SAME stepper closes it (`status="closed"`, `realized_r` set) — no new lifecycle code. Assert the closed paper trade is EXCLUDED from `load_closed_paper_trades`/the leaderboard (T1 isolation) but was advanced by `load_open_paper_trades`. A blocked limit opens no position. Idempotent (no double-open on re-run).

**Step 5 — Commit:** `feat(execution): paper adapter opens simulated positions via the existing fill engine`.

---

## Task 6: Wire the adapter into the digest (injected seam, batch-dispatched)

**Files:** `notify/run.py` (`send_digest`); `notify/body.py`/`pdf.py` (render the ticket/result); Tests.

**Step 1 — Resolve + collect.** In `send_digest`, add an injected seam param `execution_adapter: ExecutionAdapter | None = None`. Resolve once: `(mode, limits) = resolve_execution(cfg)`; `adapter = execution_adapter or _adapter_for_mode(mode, ...)` (`off`→NoOp, `paper`→PaperAdapter, `live`→NoOp+log "live is Phase 4"). When the insight engine builds an `OrderIntent` in `_deep_one`, collect `(intent, play_type)` into a per-run list (don't dispatch inline).

**Step 2 — Batch-dispatch.** After `_build_picks` (near the `score_analyst_calls` call, run.py ~355), in ONE try/except: for each collected intent, `result = adapter.submit(intent, session=session, run_date=run_date, limits=limits)`; collect results. Any exception logs + never blocks the digest (the PDF/deep-analysis seam pattern). Render an "Order ticket" line per executed pick in the body + PDF (side/limit/shares/stop/target + the `OrderResult.status`). When `mode="off"` (NoOp) → nothing dispatched/rendered beyond today's behavior.

**Step 3-4 — TDD:** with a fake adapter injected, `send_digest` collects the run's intents and calls `submit` once per deep pick (the fake records calls; it NEVER places an order); the digest renders the ticket + status; an adapter that raises does NOT break the digest. `mode="off"` (default, no adapter injected) → no submit calls, output identical to today (regression: existing digest tests green). Reuse the `send_digest` test harness/fakes.

**Step 5 — Commit:** `feat(execution): dispatch order intents through the injected adapter seam in the digest`.

---

## Task 7: The advisory autonomy gate (calibration test with teeth)

**Files:** `pipeline/reflect.py` (upgrade calibration to a TEST) or a new `analytics/calibration.py`; Create `pipeline/autonomy.py` + a CLI entry; Tests.

**Step 1 — A real calibration test.** Add `conviction_calibrated(calls, *, lower_pct=5.0) -> CalibrationVerdict` (pure): over SCORED `account`-research `AnalystCall`s, test whether `high`-conviction realized R **out-earns** `low` with TEETH — a ticker-clustered two-sample delta with a CI lower bound (reuse `analytics/performance._clustered_ci_low` / the `propose.py` two-sample machinery), plus an `n>=20` per-bucket + distinct-ticker floor. Returns `{calibrated: bool, high_minus_low, ci_low, n_high, n_low, reason}`; `calibrated=False` with `reason="insufficient data"` until the floors are met. (This upgrades the Phase-2 `analyst_calibration` bare means — keep that for the descriptive edge-file note; this adds the inferential test.)

**Step 2 — The gate.** `autonomy_gate(session, *, edge_dir) -> AutonomyReport` in `pipeline/autonomy.py`, mirroring `propose()`: reads the `forward_confirmed` verdicts sidecar (proven edge present?) + `conviction_calibrated(...)` → a structured, human-readable report (`ready: bool`, the stats, the blocking reasons). It is **read-only** — it NEVER mutates `execution_mode` or config. A `__main__`/CLI prints it (like the reflect/propose CLIs). Optionally surface a one-line status in the weekly reflection.

**Step 3-4 — TDD (teeth):** with a clean `high`-out-earns-`low` clustered sample above the floors → `calibrated=True`; a **placebo / label-shuffle** sample (conviction labels permuted) must NOT pass; below the n/cluster floor → `insufficient data`; a sample where `high` does NOT beat `low` → not calibrated. `autonomy_gate` returns `ready=False` when either the edge is unconfirmed OR conviction isn't calibrated, and never writes config. Pure tests + an in-memory DB.

**Step 5 — Commit:** `feat(execution): advisory autonomy gate + a calibration test with teeth`.

---

## Definition of done
- `ruff`/`mypy`/`pytest` green; CI green; `alembic heads` single after both migrations.
- `execution_mode` defaults `off` = **today's behavior byte-for-byte**; the suite can never place an order (the adapter is an injected fake); the kill switch is `mode=off`.
- The `paper` adapter opens `account="paper"` positions the **existing stepper** fills/closes; the intent book is **excluded from every research aggregate** (double-counting unrepresentable); `live` is unbuilt.
- Hard limits (per-day notional/loss, max-concurrent) are enforced **inside `submit()`** off a summed `ExecutionLog`; over-limit → a logged `skipped`, no position. Idempotent (no double-submit).
- The autonomy gate is **advisory-only** (never flips the mode), runs a calibration test **with teeth** (a placebo can't pass), and reports `"insufficient data"` until the floors are met.

## Out of scope (Phase 4+)
The live broker (Robinhood agentic/MCP) adapter + OAuth token storage; auto-flipping `execution_mode`; intra-run halting via a DB/file flag; the analyst proposing its own variants. Autonomy is switched on **last, by a human**, behind the gate + limits + kill switch.

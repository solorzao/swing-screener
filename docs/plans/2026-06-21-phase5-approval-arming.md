# Phase 5 — Robinhood approval surface + Alpaca arming readiness Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship a money-safe **Robinhood human-approval surface** (a `manual` mode + a "Proposed orders" rendering you act on by hand — usable now) and make the Alpaca real-money flip **armable-when-ready** (a read-only preflight GO/NO-GO, the autonomy-gate countdown, and an arming runbook).

**Architecture:** A new `manual` `execution_mode` selects the existing Phase-3 `ManualAdapter` (records the order ticket, moves no money, contacts no broker); a consolidated "Proposed orders" section + structured artifact renders the day's tickets as Robinhood-placeable instructions. For arming readiness: `BrokerClient` gains `get_account`; a read-only `preflight` CLI prints a go-live checklist; the autonomy gate's existing floor stats are surfaced as a *countdown*; a runbook documents the ceremony. **Nothing here arms execution or moves money.**

**Tech Stack:** Python 3.12, httpx (already a dep), SQLAlchemy, pytest. No new deps.

**Governed by:** [North Star](../NORTH_STAR.md) (#1/#3/#6) + the [Phase-5 design](2026-06-21-phase5-approval-arming-design.md). Builds on Phase 0-4.

---

## Design invariants (hold for every task)
- **Nothing arms execution or moves money.** Default `execution_mode="off"`; `manual` records tickets only (no broker, no positions); real money still needs all three locks; preflight + the countdown are READ-ONLY; Robinhood holds NO credentials and makes NO API call.
- **Fail-safe mode parsing.** An unknown/garbage `execution_mode` still coerces to `off` (the existing guard); `manual` joins the valid set explicitly.
- **No live network in CI.** Preflight + `get_account` are tested via `FakeBroker` / `httpx.MockTransport`.
- **Commit discipline:** gate on green (`ruff check .`, `mypy`, `pytest -q`) BEFORE each commit. No `--no-verify`. Trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## Task 1: The `manual` execution mode

**Files:** `settings.py` (`_EXECUTION_MODES`); `notify/run.py` (`_adapter_for_mode`); Tests.

**Step 1 — Settings.** Add `"manual"` to `_EXECUTION_MODES` (settings.py:67) so it's a valid mode (`{off, manual, paper, live}`); the unknown→off fail-safe (settings.py:136) is unchanged. `resolve_execution` returns `"manual"` like any mode (it already passes the mode through).

**Step 2 — Adapter resolution.** In `_adapter_for_mode(mode, *, broker=None)` (notify/run.py:221): `"manual"` → `ManualAdapter()` (it needs no broker). `paper`→PaperAdapter, `live`→LiveAdapter(broker) or NoOp+warn, else NoOp. In `send_digest`, `manual` builds NO broker (only `live` does at run.py:321) — a manual run never touches a broker.

**Step 3 — TDD** (`tests/notify/test_run_manual_mode.py`): with `execution_mode="manual"` + a deep playbook pick, the digest dispatches the `ManualAdapter` → a `"recorded"` ExecutionLog ticket (account `"manual"`), **no PaperTrade, no broker constructed** (assert `build_broker` not called / inject a sentinel), and the order-ticket renders. `off` (default) unchanged. The per-submit kill switch (`_execution_halted`) is a no-op for the non-live ManualAdapter.

**Step 4 — Commit:** `feat(approval): manual execution mode selects the order-ticket adapter`.

---

## Task 2: The "Proposed orders" surface (Robinhood-placeable)

**Files:** `notify/run.py` + `notify/body.py`/`pdf.py` (a consolidated section); a structured artifact writer; Tests.

**Step 1 — The consolidated section.** When `execution_mode="manual"` and tickets were recorded, render a dedicated **"Proposed orders — place on Robinhood"** section in the digest body + PDF: one line per pick with the exact, human-placeable instruction derived from the `OrderTicketLine` / `OrderIntent` — `"Buy AMD — limit <= $101.00, 40 shares (HIGH conviction); then set stop $95.00, target $110.00"`. This is a *summary list* (distinct from the existing inline per-pick ticket), so the user has one actionable "shopping list".

**Step 2 — A structured artifact.** Write the same proposals to a small structured file alongside the PDF (e.g. `proposed_orders_<run_date>.json` in the digest dir): a list of `{ticker, side, limit_price, shares, stop, target, conviction, play_type}`. (This is the seed for a future Robinhood `review→place` export — keep it a plain JSON list now.) Reuse the recorded tickets / intents; do NOT recompute levels.

**Step 3 — TDD:** with `manual` mode + 2 deep picks, the digest body/PDF contain the "Proposed orders" section with each pick's exact spec; the JSON artifact is written with the right fields. When mode != manual (or no tickets) → no section, no artifact (regression: existing digest tests green). Reuse the digest test harness.

**Step 4 — Commit:** `feat(approval): consolidated "Proposed orders" surface + structured artifact`.

---

## Task 3: `BrokerClient.get_account` + the preflight GO/NO-GO CLI

**Files:** `pipeline/broker.py` (`BrokerAccount` + `get_account` on the Protocol + `FakeBroker`); `pipeline/broker_alpaca.py` (`get_account`); Create `pipeline/preflight.py`; Tests.

**Step 1 — `get_account`.** Add a frozen `BrokerAccount{cash: float, buying_power: float, status: str}` + `get_account(self) -> BrokerAccount` to the `BrokerClient` Protocol. `FakeBroker`: a constructor knob (`cash`/`buying_power`/`status`, defaults to a funded ACTIVE account) returning it. `AlpacaBroker.get_account`: `GET /v2/account` → parse `cash`/`buying_power` (string→float) + `status`. Tested via `httpx.MockTransport` (no live network).

**Step 2 — Preflight (READ-ONLY).** `pipeline/preflight.py`: `preflight(session, settings, *, broker) -> PreflightReport{go: bool, checks: list[(name, ok, detail)]}`. Checks: (1) `execution_mode`/`broker` configured for the intended target; (2) broker reachable — `get_account()` succeeds (a broker error → a NO-GO line, never raises); (3) account funded + `status == "ACTIVE"` + `buying_power > 0`; (4) `real_money_limits_ok(limits)` — all caps set; (5) `is_real_money()` matches the configured host (warn if a live host is configured); (6) the `autonomy_gate(session).ready` status (advisory — surfaced, not a hard gate). `go = all(safety-critical checks)`. It performs **NO writes and never arms**. A `render_preflight(report) -> str` + a `__main__` CLI that prints the checklist (mirror the autonomy/reflect CLIs) with a "this does not arm anything" disclaimer.

**Step 3 — TDD** (`tests/pipeline/test_preflight.py`): with a `FakeBroker` (funded/ACTIVE) + all caps set + a stubbed gate → `go=True`, every check ok; an unfunded/`status!="ACTIVE"` broker → NO-GO on the funding line; a missing cap → NO-GO on the caps line; a raising broker → NO-GO (reachability), never raises; assert `preflight` writes NOTHING (no DB/settings mutation). `AlpacaBroker.get_account` parses the account JSON (MockTransport). NO live network.

**Step 4 — Commit:** `feat(arming): broker get_account + read-only preflight GO/NO-GO check`.

---

## Task 4: The autonomy-gate countdown

**Files:** `pipeline/autonomy.py` (extend `render_report` + a countdown helper); `notify/run.py` (a one-line digest status); Tests.

**Step 1 — The countdown (pure).** A pure `gate_countdown(report) -> str` (in autonomy.py): per play type, format progress toward the floors from each `CalibrationVerdict` — `n_high`/`n_low` vs `MIN_LEADERBOARD_N` (20) and `n_clusters_high`/`n_clusters_low` vs `_CLUSTER_FLOOR` (8): e.g. `"continuation: 7/20 high, 3/20 low, 5/8 tickers"`. (The fields + floors already exist — `calibration.py` imports `MIN_LEADERBOARD_N`/`_CLUSTER_FLOOR`.)

**Step 2 — Surface it.** Add the countdown to `render_report` (a "## Calibration progress" block) AND a one-line status in the digest (e.g. a footer line when deep analysis is on: `"Autonomy gate: NOT READY — continuation 7/20 high, 3/20 low, 5/8 tickers"`), so the gate can be *watched* approaching. Read-only; computed from the same `autonomy_gate` call.

**Step 3 — TDD:** `gate_countdown` formats the progress per play type from a built `AutonomyReport` (assert the "X/20"/"Y/8" strings); the digest renders the gate status line when deep analysis is on (mock the gate); off/no-deep → no line. Pure countdown test + a digest-surfacing test.

**Step 4 — Commit:** `feat(arming): surface the autonomy-gate countdown in the report + digest`.

---

## Task 5: The Alpaca-live arming runbook + go-live verification

**Files:** Create `docs/runbooks/arming-alpaca-live.md`; Tests (verification of the live-money guards); maybe a tiny `is_real_money`/caps assertion.

**Step 1 — The runbook.** `docs/runbooks/arming-alpaca-live.md`: the ordered, explicit ceremony to flip to Alpaca live — (1) run `preflight` and confirm GO; (2) confirm `autonomy_gate` is `ready` (the countdown reads 20/20 + 8 tickers); (3) set the Alpaca **live** host + `SWING_ALPACA_KEY/SECRET` (live) + ALL caps (`SWING_MAX_DAILY_NOTIONAL`/`_LOSS`/`_CONCURRENT`) + `SWING_BROKER_ALLOW_REAL_MONEY=yes`; (4) fund the account; (5) a **kill-switch drill** (flip `execution_mode` off mid-run, confirm `cancel_all_orders`); (6) flip `execution_mode=live`; (7) monitor + the rollback (set `off`). Document the three locks + that each is required.

**Step 2 — Go-live verification (tests).** Add/confirm the explicit guards the runbook relies on: `AlpacaBroker.is_real_money()` is `True` for the live host (`api.alpaca.markets`) and `False` for paper (extend the P4-T6 test if not already explicit); a real-money endpoint with an UNSET cap is refused (the `real_money_limits_ok` path in `LiveAdapter` — confirm a test asserts a real-money broker + a None cap → `rejected_live`, no order). These pin the safety the runbook promises.

**Step 3 — TDD:** the is_real_money(live/paper) assertions; the live-without-caps refusal (FakeBroker(real_money=True) + a None cap → rejected, no order). Doc has no test (prose), but the verification tests are the teeth behind it.

**Step 4 — Commit:** `docs(arming): Alpaca-live arming runbook + go-live safety verification`.

---

## Definition of done
- `ruff`/`mypy`/`pytest` green; CI green.
- **Approval surface (usable now):** `execution_mode=manual` records order tickets (no broker, no positions, no money) and renders a consolidated "Proposed orders" section + a JSON artifact you act on by hand. `off` stays the default.
- **Arming readiness:** a read-only `preflight` GO/NO-GO CLI (broker reachable / funded / caps set / is_real_money / gate status), the autonomy-gate **countdown** surfaced in the report + digest, and an arming runbook — all read-only.
- **Nothing arms execution or moves money;** real money still needs all three locks; Robinhood holds no creds and makes no API call; no live network in CI.

## Out of scope (Phase 6+)
A true Robinhood `review → place` MCP export/integration; placed-vs-skipped proposal tracking; the actual real-money arming flip (a deliberate human act once the gate passes); the partial/fractional fill model; other headless brokers. Auto-arming is never in scope.

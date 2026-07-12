# Journal v2 — The Two Coaches — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan
> task-by-task. Work in the worktree `.claude/worktrees/journal-v2` on branch `feat/journal-v2`.

**Goal:** Ship Journal v2 — a Personal Trade Coach over Oliver's real manual trades and an
independent System Behavior Auditor over the agents' conduct — per the approved design at
[2026-07-12-journal-v2-two-coaches-design.md](2026-07-12-journal-v2-two-coaches-design.md).

**Architecture:** Two LLM review surfaces that never share data, prompt, or voice. Deterministic
graders own every number (pure `journal/` modules); Opus writes only advisory prose behind the
`author_edge_file` firewall (degrade-to-template on any failure). Personal reviews hang off the
manual-close event (facts sync, LLM draft async via a copy of the `analysis_requests` queue). Both
surfaces are default-off, per-surface USD-capped, and human-gated. New DB tables chain off Alembic
head `a4e7c1b9f2d6`. Cockpit gets Coach panels inside JOURNAL and a new SYSTEM AUDIT screen.

**Tech Stack:** Python 3.14 / SQLAlchemy 2 (`Mapped`/`mapped_column`) / Alembic / FastAPI factory
routers / pytest (in-memory sqlite) / anthropic SDK (lazy) / Vite+React+TS cockpit / Azure
Container Apps Jobs (bicep).

**Conventions (read once):**
- **Type-check the worktree the CI way** (per memory `feedback-mypy-worktree-gap`): bare `mypy`
  checks *main's* src via the editable install. Always run
  `MYPYPATH=src /c/Users/Oliver/source/repos/swing-screener/.venv/Scripts/python.exe -m mypy src/swing_screener --no-incremental` **from the worktree dir**.
- Run tests with the shared venv from the worktree dir:
  `/c/Users/Oliver/source/repos/swing-screener/.venv/Scripts/python.exe -m pytest <paths> -q`
  (pyproject sets `pythonpath=["src"]`, so pytest exercises the worktree src).
- Commit after every green task. Never push/PR (Oliver decides that).
- **Firewall rule, everywhere an LLM is called:** code writes `facts_json`/`findings_json` FIRST and
  it is authoritative; the LLM only writes `narrative`; on ANY exception (incl. empty reply) degrade
  to a deterministic template. Copy `author_edge_file` (reflect.py:664-737) — lazy `import anthropic`,
  `client` injectable seam, fence the facts as `<<<GROUND_TRUTH_SCAFFOLD ...`.
- **Never confuse** `ExecutionLog.account="manual"` (agent manual-mode ticket) with the `Trade` table
  (the human book). **Never scope `ExitEvent` by `account`** for the Auditor — a manual close is
  `account="research", is_paper=False`; filter on `is_paper`.

---

## Group 0 — Foundation (serial; everything depends on this)

### Task 1: New DB models + `trades.emotional_state` column

**Files:**
- Modify: `src/swing_screener/db/models.py` (add classes after the existing journal tables ~L479; add one column to `Trade` ~L91)
- Test: `tests/db/test_journal_v2_models.py` (create)

**Step 1 — failing test.** Assert the four new tables + the new column exist and round-trip via
`create_all` on an in-memory engine, and that the composite uniques reject a duplicate.

```python
# tests/db/test_journal_v2_models.py
from datetime import date, datetime
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from swing_screener.db.models import (
    JournalReview, SystemAudit, WeaknessesProfile, DisarmEvent, Trade,
)
from swing_screener.db.session import get_engine

def test_journal_review_roundtrip_and_unique():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(JournalReview(kind="trade_close", book="manual_equity", trade_id=1,
                            facts_json="{}", source="analyst"))
        s.commit()
        s.add(JournalReview(kind="trade_close", book="manual_equity", trade_id=1,
                            facts_json="{}", source="analyst"))
        with pytest.raises(IntegrityError):
            s.commit()

def test_trade_emotional_state_column():
    with Session(get_engine("sqlite:///:memory:")) as s:
        t = Trade(ticker="AMD", timeframe="1d", horizon="medium", entry_date=date(2026,7,1),
                  entry_price=100.0, size=1.0, stop=95.0, target=110.0, emotional_state="calm")
        s.add(t); s.commit(); s.refresh(t)
        assert t.emotional_state == "calm"
```

**Step 2 — run, expect fail** (`ImportError`/no column):
`.../python.exe -m pytest tests/db/test_journal_v2_models.py -q`

**Step 3 — implement.** In `models.py`, add `emotional_state: Mapped[str | None] = mapped_column(String(32), default=None)` to `Trade` (after `override`, L91). Add the four models using the exact idioms from the grounding (Text for JSON, `UniqueConstraint` in `__table_args__` with trailing comma, nullable usage cols copied from `AnalystCall` L250-253):

```python
class JournalReview(Base):
    """Coach A artifact: one per-trade review or weekly rollup. facts_json is
    authoritative (code-written); narrative is advisory LLM prose."""
    __tablename__ = "journal_reviews"
    __table_args__ = (
        UniqueConstraint("kind", "book", "trade_id", "covered_from", "covered_to",
                         name="uq_journal_reviews_identity"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))            # trade_close | weekly_rollup
    book: Mapped[str] = mapped_column(String(16), index=True)
    trade_id: Mapped[int | None] = mapped_column(default=None)   # null for rollups
    covered_from: Mapped[date | None] = mapped_column(default=None)
    covered_to: Mapped[date | None] = mapped_column(default=None)
    facts_json: Mapped[str] = mapped_column(Text, default="{}")
    narrative: Mapped[str | None] = mapped_column(Text, default=None)
    human_edit: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[str] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    input_tokens: Mapped[int | None] = mapped_column(default=None)
    output_tokens: Mapped[int | None] = mapped_column(default=None)
    est_cost_usd: Mapped[float | None] = mapped_column(default=None)
    generated_at: Mapped[datetime | None] = mapped_column(default=None)

class SystemAudit(Base):
    """Auditor B artifact: weekly sweep or immediate breach. findings_json authoritative."""
    __tablename__ = "system_audits"
    __table_args__ = (
        UniqueConstraint("kind", "period_from", "period_to", "breach_key",
                         name="uq_system_audits_identity"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))            # weekly | breach
    period_from: Mapped[date] = mapped_column()
    period_to: Mapped[date] = mapped_column()
    breach_key: Mapped[str] = mapped_column(String(64), default="")   # "" for weekly; dedup id for breach
    findings_json: Mapped[str] = mapped_column(Text, default="{}")
    severity: Mapped[str] = mapped_column(String(16), default="info")
    narrative: Mapped[str | None] = mapped_column(Text, default=None)
    acknowledged_by_human: Mapped[bool] = mapped_column(default=False)
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    input_tokens: Mapped[int | None] = mapped_column(default=None)
    output_tokens: Mapped[int | None] = mapped_column(default=None)
    est_cost_usd: Mapped[float | None] = mapped_column(default=None)
    generated_at: Mapped[datetime | None] = mapped_column(default=None)

class WeaknessesProfile(Base):
    """Living distillation (append-only; latest row is current). items_json evidence
    keys on (book, trade_id) pairs — Coach id spaces overlap across producers."""
    __tablename__ = "weaknesses_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), default="personal")
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    covered_from: Mapped[date | None] = mapped_column(default=None)
    covered_to: Mapped[date | None] = mapped_column(default=None)
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    generated_at: Mapped[datetime | None] = mapped_column(default=None)

class DisarmEvent(Base):
    """Persisted so the Auditor can see an unexpected disarm (POST /api/disarm today logs only)."""
    __tablename__ = "disarm_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column()
    reason: Mapped[str] = mapped_column(String(256), default="")
    orders_cancelled: Mapped[int] = mapped_column(default=0)
```

**Step 4 — run, expect pass.** **Step 5 — commit** `feat(journal-v2): add JournalReview/SystemAudit/WeaknessesProfile/DisarmEvent models + Trade.emotional_state`.

### Task 2: Alembic migration for the v2 tables + column

**Files:**
- Create: `alembic/versions/b5f8d2a1c3e7_journal_v2_tables.py` (pick any new 12-hex revision id)
- Test: `tests/db/test_migration_journal_v2.py` (create)

**Step 1 — failing test.** Assert the single head is the new revision and `upgrade` then `downgrade`
runs clean on a temp sqlite file (copy the shape of any existing `tests/db/test_migration_*.py`; if
none, drive `command.upgrade(cfg, "head")` / `command.downgrade(cfg, "-1")` against a temp file DB).
Also assert `ScriptDirectory.from_config(cfg).get_heads() == ["b5f8d2a1c3e7"]`.

**Step 2 — run, expect fail.**

**Step 3 — implement.** Header: `down_revision = "a4e7c1b9f2d6"` (confirmed current head). `upgrade()`
does `op.create_table(...)` for the four tables (nullable→`nullable=True` no server_default; Text
JSON→`nullable=False, server_default="{}"`/`"[]"`; bool→`nullable=False, server_default=sa.false()`),
`op.create_index("ix_journal_reviews_book", ...)`, the two unique indexes
(`op.create_index("uq_journal_reviews_identity", ..., unique=True)` etc.), and
`op.add_column("trades", sa.Column("emotional_state", sa.String(length=32), nullable=True))`
(mirror `e4b8a2d6f1c9_trade_override`). `downgrade()` drops them in reverse.

**Step 4 — run, expect pass.** **Step 5 — commit** `feat(journal-v2): migration b5f8d2a1c3e7 for v2 tables + trades.emotional_state`.

### Task 3: Settings — per-surface default-off flags + USD caps

**Files:**
- Modify: `src/swing_screener/settings.py` (Settings dataclass ~L41-50; `load_settings` ~L163-169)
- Test: `tests/test_settings.py` (add cases)

**Steps (TDD):** Add `coach_enabled: bool`, `coach_max_usd: float | None`, `audit_enabled: bool`,
`audit_max_usd: float | None`. Parse: `coach_enabled=(env.get("SWING_COACH_ENABLED","").strip().lower() in _TRUE)`
(absent ⇒ off), `coach_max_usd=_opt_float(env.get("SWING_COACH_MAX_USD"))` (missing/garbage ⇒ None ⇒
uncapped, acceptable only because the surface is default-off), same for audit. Test: unset ⇒
`enabled False, max_usd None`; `SWING_COACH_ENABLED=1` + `SWING_COACH_MAX_USD=1.50` ⇒ `True, 1.5`;
garbage max ⇒ None. **Commit** `feat(journal-v2): default-off coach/audit settings + USD caps`.

---

## Group A — Read-model re-point (prerequisite for the Coach UI)

### Task 4: Generalize `TradeRecord.r` → `result`

**Files:**
- Modify: `src/swing_screener/journal/record.py:49-100` (dataclass + producer)
- Modify: `src/swing_screener/cockpit/routers/journal.py:306-323` (`_record_dict`: `rec.r` → `rec.result`)
- Test: `tests/journal/test_record.py:56,68` (`.r` → `.result`)

**Steps (TDD, one atomic commit — the rename touches 3 files):** Rename the dataclass field `r:
float | None` to `result: float | None`; set it from `t.realized_r` in the existing swing producer.
Update the serializer and the test asserts. Decide the wire key stays `"r"` for FE compatibility
(only `_record_dict` serializes it; keep the JSON key `"r"`, read from `rec.result`). Run
`tests/journal/ tests/cockpit/` green. **Commit** `refactor(journal): generalize TradeRecord.r -> result (unit tag interprets)`.

### Task 5: `manual_equity` producer (Trade table, computed R)

**Files:** Modify `src/swing_screener/journal/record.py` (add `manual_equity_records`); Test
`tests/journal/test_record.py`.

**Steps (TDD):** Golden test builds two `Trade` rows (one closed target, one open) + a machine-book
`PaperTrade`, asserts `manual_equity_records(s)` returns only the Trade rows with computed R
(`(exit-entry)/(entry-stop)`, `risk<=0 → None`), `book="manual_equity"`, `unit="R"`, and overlay
tags joined under `book="manual_equity"`. Implement per grounding skeleton §2b — select the WHOLE
`Trade` table (no `.where(account)`), reuse `_tags_by_trade`/`_theses_by_trade`. **Commit**
`feat(journal): manual_equity TradeRecord producer over the Trade table`.

### Task 6: `robinhood` producer (OptionPaperTrade, `$` unit)

**Files:** Modify `record.py`; Test `tests/journal/test_record.py`.

**Steps (TDD):** Golden test builds an `OptionPaperTrade(account="robinhood", ...)` with
`opened_at`/`closed_at` datetimes and a `premium_pnl`, plus an `account="options-lab"` row that must
NOT appear. Assert `robinhood_records(s)` returns only robinhood rows with `unit="$"`,
`result=premium_pnl`, `opened/closed` coerced via `.date()`, `module="gex"`. Implement per skeleton
§2c (mandatory `.where(account=="robinhood")` guard). **Commit** `feat(journal): robinhood TradeRecord producer ($ premium, never R)`.

### Task 7: Zero-leak regression test (the design's §Boundary guarantee)

**Files:** Test `tests/journal/test_boundary_isolation.py` (create).

**Steps (TDD):** Seed a mixed DB (Trade rows, a robinhood OptionPaperTrade, an options-lab
OptionPaperTrade, PaperTrade research/paper/live). Assert: `manual_equity_records` + `robinhood_records`
return ZERO rows whose book ∈ machine set; the swing `trade_records(book="research")` returns ZERO
Trade/robinhood rows. This test is the executable form of the distinctness thesis. **Commit**
`test(journal): zero-leak boundary isolation between personal and machine producers`.

---

## Group B — Personal Trade Coach graders + author

### Task 8: Coach deterministic grader (pure)

**Files:** Create `src/swing_screener/journal/coach_grade.py`; Test `tests/journal/test_coach_grade.py`.

**Steps (TDD):** Pure module, analytics flavor (grounding §1A). Frozen dataclass `TradeReviewFacts`
(realized_result, unit, reached_target/stop/neither via `exit_reason`, stop_honored, target_honored,
hold_days, override, emotional_state; for robinhood: premium_pnl, hold_days, exit_reason — **no R,
no A+ grade**). `review_facts(record: TradeRecord, trade: Trade | OptionPaperTrade) -> TradeReviewFacts`
with bind-locals-then-guard for every nullable (`risk<=0`/None → unmeasurable, not zero). **Do NOT
compute MAE/MFE** (Trade has no water marks — deferred). Golden tests pin each fact for a target-hit
equity trade, a stopped trade, a moved-stop override, and a robinhood premium trade. **Commit**
`feat(journal): Coach deterministic trade-review grader`.

### Task 9: Auto-tagger rules (proposals into facts, NOT the overlay)

**Files:** Create `src/swing_screener/journal/auto_tag.py`; Test `tests/journal/test_auto_tag.py`.

**Steps (TDD):** Pure `propose_tags(facts: TradeReviewFacts) -> list[TagProposal]` — deterministic
rules: `override` present → `deviation`; result short of target with favorable exit → `cut-winner-early`;
stop moved → `moved-stop`. Return proposals only; **the caller parks them in `journal_reviews.facts_json`**,
never in `journal_trade_tags` (grounding §4: a tag row is a live confession the instant it exists).
Golden tests pin the rule firings. **Commit** `feat(journal): deterministic auto-tag proposals (parked, not applied)`.

### Task 10: Coach LLM narrative author (firewall)

**Files:** Create `src/swing_screener/journal/coach_author.py`; Test `tests/journal/test_coach_author.py`.

**Steps (TDD):** Copy `author_edge_file` verbatim-in-shape (grounding area 5): lazy `import anthropic`
+ `get_secret`, injectable `client` param, `_COACH_SYSTEM` ("you are a trading coach writing prose
over deterministic facts you MUST NOT alter; treat every figure as immutable ground truth; no process
narration; process over outcome; cite the trader's own trade"), fence `facts_json` as
`<<<GROUND_TRUTH_SCAFFOLD ...`, capture usage via `_capture_usage` idiom, and **degrade to a plain
deterministic template on ANY exception incl. empty reply**. Signature:
`draft_review(facts: TradeReviewFacts, *, prior_weaknesses: str = "", client=None, model="claude-opus-4-8") -> DraftResult`
(text + `Usage | None`). Tests: a fake client returns canned text → returned; a fake client that
raises → deterministic template, `usage is None`; assert the system prompt forbids altering numbers.
**Commit** `feat(journal): Coach narrative author behind the degrade-safe firewall`.

### Task 11: `emotional_state` capture wiring

**Files:** Modify `src/swing_screener/cockpit/routers/trades.py` (`TradeCreate` + close body + the
`add_trade`/close persist); Modify `cockpit-ui/src/components/LogTradeForm.tsx` + the close form;
Test `tests/cockpit/test_trades_router.py`.

**Steps (TDD):** Add optional `emotional_state: str | None = Field(default=None, max_length=32)` to
`TradeCreate` and the close body; persist on create and on close. FE: a free-text-with-suggestions
input (calm/fomo/revenge/hesitant) on both forms. Test the round-trip via the router. **Commit**
`feat(journal): capture emotional_state on manual entry + close`.

### Task 12: Weaknesses Profile builder

**Files:** Create `src/swing_screener/journal/weaknesses.py`; Test `tests/journal/test_weaknesses.py`.

**Steps (TDD):** `build_profile(session) -> WeaknessesProfile` — distill recurring tags/flags across
the week's `journal_reviews`, evidence as `(book, trade_id)` pairs (grounding gotcha: ids overlap),
stamp `generated_at` + covered window, append a new row (latest = current). Thin-data honesty: if the
closed-trade count is below a floor, emit a labelled sparse profile, not silence. LLM prose optional
via the Task-10 author (default-off). Golden test with a seeded review set. **Commit**
`feat(journal): staleness-stamped Weaknesses Profile builder`.

---

## Group C — On-close async draft queue

### Task 13: `CoachDraftRequest` queue model + repo + migration

**Files:** Modify `models.py` (copy `AnalysisRequest` L310 shape, swap `ticker`→`trade_id:int`+`book:str`,
drop pdf/chart cols); Modify `repo.py` (copy `create_/claim_/requeue_stale_/complete_/fail_` L661-737
as `*_coach_draft_request`); Create migration chaining off `b5f8d2a1c3e7`; Tests `tests/db/`.

**Steps (TDD):** Golden test the claim race-safety (queued→running, self-identifying read-back by
`started_at==now`) and stale-requeue. Keep BOTH the UPDATE and the `started_at==now` filter (grounding
gotcha). **Commit** `feat(journal): CoachDraftRequest queue (model + repo + migration)`.

### Task 14: Enqueue the draft on manual close (sync facts + async draft)

**Files:** Modify `src/swing_screener/cockpit/routers/trades.py:236` (`close_trade_action`); Test
`tests/cockpit/test_trades_router.py`.

**Steps (TDD):** After the existing `close_trade_with_event` call, in the same request: (1) build the
`TradeRecord` + `review_facts`, write the synchronous `JournalReview(kind="trade_close",
book="manual_equity", facts_json=..., source="analyst", narrative=None)` row; (2)
`create_coach_draft_request(session, trade_id=..., book="manual_equity", requested_at=datetime.now(UTC))`;
(3) keep `action_nonce.bump()`. HTTP close stays instant; no LLM here. Test asserts the facts row +
queue row exist after a close, and no Anthropic call happens on the HTTP path. **Commit**
`feat(journal): on manual close, write sync review facts + enqueue async draft`.

### Task 15: Coach drain worker + rollup CLI (`journal.coach_run`)

**Files:** Create `src/swing_screener/journal/coach_run.py`; Test `tests/journal/test_coach_run.py`.

**Steps (TDD):** Copy `ondemand.py` drain shape (grounding §1d): `process_pending` (requeue_stale
FIRST → claim → `process_one` each), `process_one` **never raises** (persist `type(exc).__name__`
only). `process_one` drains one draft: load facts from the `JournalReview` row, call `draft_review`
(Task 10) under the **spend accumulator** (`spend[0]`, `over_ceiling` vs `settings.coach_max_usd`,
grounding §4a), backfill `narrative` + usage cols. `main()` (single-command argparse, run.py shape)
does BOTH the drain AND `build_profile` (Task 12) weekly rollup; guard prod DB with the
`options/run.py` cloud-sqlite refusal. Tests: a drained request backfills narrative; a raising author
fails-the-row not the batch; the ceiling stops LLM calls and leaves later rows facts-only. **Commit**
`feat(journal): coach_run worker (drain on-close drafts + weekly rollup) with spend ceiling`.

---

## Group D — System Behavior Auditor

### Task 16: Disarm-event persistence (prerequisite)

**Files:** Modify `src/swing_screener/cockpit/routers/safety.py:87` (`POST /api/disarm`); Test
`tests/cockpit/test_safety_router.py`.

**Steps (TDD):** After the existing cancel/re-submit, write a `DisarmEvent(created_at=datetime.now(UTC),
reason=..., orders_cancelled=n)` in the same txn; keep the nonce bump. Test the row is written.
**Commit** `feat(safety): persist a DisarmEvent so the Auditor can see unexpected disarms`.

### Task 17: Auditor compliance grader (pure)

**Files:** Create `src/swing_screener/journal/audit_compliance.py`; Test `tests/journal/test_audit_compliance.py`.

**Steps (TDD):** Pure functions over machine data only: cap-adherence (per-day notional/loss sums from
`ExecutionLog` vs mandate), rejected/clamped rate (`ExecutionLog.status ∈ {rejected_live,rejected,skipped}`),
config churn (`proposals.py` approve/withdraw counts, flag config-change-after-drawdown), disarm count
(`DisarmEvent`). **`Trade.override` is off-limits** — assert no import of `Trade`. Golden tests seed
`ExecutionLog` rows and pin each metric. **Commit** `feat(journal): Auditor compliance grader (caps, rejects, config churn, disarm)`.

### Task 18: Auditor anomaly grader (pure) + zero-leak test

**Files:** Create `src/swing_screener/journal/audit_anomaly.py`; Test `tests/journal/test_audit_anomaly.py`.

**Steps (TDD):** Pure: surfacing drought + `would_surface` leaks (`ReversalFunnel`), calibration drift
(`AnalystCall` baseline-vs-final, `nudge_vs_baseline_r` — reuse `reflect.analyst_calibration`),
integrity (duplicate idempotency keys; `ExitEvent` filtered `is_paper=True`, **never by account**).
Zero-leak test: seed a `manual_close` ExitEvent + a robinhood row and assert the auditor context sees
NEITHER. **Commit** `feat(journal): Auditor anomaly grader + machine/personal zero-leak test`.

### Task 19: Auditor LLM author + `journal.audit_run` CLI (weekly + breach)

**Files:** Create `src/swing_screener/journal/audit_author.py` + `src/swing_screener/journal/audit_run.py`;
Tests alongside.

**Steps (TDD):** `audit_author.draft_audit(findings, *, client=None, ...)` — same firewall as Task 10,
distinct `_AUDIT_SYSTEM` voice (a third-party conduct auditor, **not** the analyst; must not import
analyst context). `audit_run.main()` subcommand CLI (`weekly` = sweep → write `SystemAudit(kind="weekly")`
+ optional prose under `audit_max_usd`; `breach` = daily scan for new cap-exceed/disarm since last,
write `SystemAudit(kind="breach", severity=..., breach_key=...)`). Weekly writes are idempotent on the
unique key. Test both paths + the spend ceiling + degrade. **Commit** `feat(journal): audit_run CLI (weekly sweep + daily breach scan) + author`.

---

## Group E — Spend telemetry

### Task 20: Widen `_spend` + `analyst_spend_today` to UNION the new tables

**Files:** Modify `src/swing_screener/cockpit/routers/analyst.py:127` and
`src/swing_screener/cockpit/routers/safety.py:74`; Tests alongside.

**Steps (TDD):** Extend both SELECTs to UNION `est_cost_usd` across `AnalystCall` + `journal_reviews`
+ `system_audits`, keeping the NULL-counted-not-summed disclosure. Test that a `journal_reviews` row
with a cost shows up in the window total. **Commit** `feat(cockpit): spend telemetry unions coach/audit costs (no under-report)`.

---

## Group F — Cockpit backend routers

### Task 21: `build_coach_router` + register + SSE watermark

**Files:** Create `src/swing_screener/cockpit/routers/coach.py`; Modify `cockpit/api.py:~200`
(`include_router`) and `cockpit/routers/events.py:163` (watermark); Test `tests/cockpit/test_coach_router.py`.

**Steps (TDD):** Factory `build_coach_router(*, _session, action_nonce)` (grounding area 3 §1):
`GET /api/coach/reviews?book=`, `GET /api/coach/weaknesses`, `POST /api/coach/reviews/{id}/edit`
(human_edit), `POST /api/coach/reviews/{id}/confirm-tag` (writes the parked proposal to the overlay
via `repo.tag_trade(source="analyst")` — the confirm gate). `_require_cockpit` on writes,
`action_nonce.bump()` after commit, `_num` for nullable floats, ISO/`_utc_iso` for dates. Register in
`api.py`; add SSE keys `coach_reviews` = `max(id)` piped with a `count(narrative IS NOT NULL)` UPDATE
clock (reviews get backfilled). Contract-test each endpoint + the header guard. **Commit**
`feat(cockpit): coach router (reviews, weaknesses, edit, confirm-tag) + SSE`.

### Task 22: `build_audit_router` + register + SSE watermark

**Files:** Create `src/swing_screener/cockpit/routers/audit.py`; Modify `api.py`, `events.py`; Test
`tests/cockpit/test_audit_router.py`.

**Steps (TDD):** `GET /api/audit/reports`, `GET /api/audit/breaches`, `POST /api/audit/{id}/ack`
(sets `acknowledged_by_human`). Same guards. SSE `system_audits` = `max(id)` piped with
`count(acknowledged_by_human IS TRUE)` (ack is an UPDATE). Contract-test. **Commit**
`feat(cockpit): audit router (reports, breaches, ack) + SSE`.

---

## Group G — Cockpit frontend

### Task 23: api.ts wire types + getters/posters + JournalBook

**Files:** Modify `cockpit-ui/src/api.ts` (`JournalBook` L1041 adds `manual_equity`/`robinhood`; add
`CoachReview`/`WeaknessesItem`/`AuditReport` types + `getCoachReviews`/`postCoachEdit`/
`postConfirmTag`/`getWeaknesses`/`getAuditReports`/`postAuditAck` via `fetchJson`/`postAction`).

**Steps:** No test (types); `tsc` build in Task 26 covers it. **Commit** `feat(cockpit-ui): coach/audit wire types + api helpers`.

### Task 24: JournalScreen — personal books + Coach panels

**Files:** Modify `cockpit-ui/src/screens/JournalScreen.tsx`.

**Steps:** Extend `BOOK_OPTIONS` with `manual_equity` + `robinhood` (grounding area 3 §4). Add a
`CoachReviewsPanel` and a `WeaknessesPanel`, **gated `book === 'manual_equity' || book === 'robinhood'`**
(Coach voice never over a machine book). Reuse the `.panel`/`PanelBody`/`StatChip`/`usePolling(...book)`
idioms; robinhood values are `$`-unit via `stat.unit`. **Commit** `feat(cockpit-ui): Coach panels in JOURNAL (personal-book gated)`.

### Task 25: SystemAuditScreen + the digitless 3-touch

**Files:** Create `cockpit-ui/src/screens/SystemAuditScreen.tsx`; Modify `cockpit-ui/src/lib/screens.ts`,
`cockpit-ui/src/App.tsx`, `cockpit-ui/src/components/Masthead.tsx`.

**Steps:** Add `'systemaudit'` to `ScreenId` + a `{ id:'systemaudit', digit:null, title:'SYSTEM AUDIT' }`
registry row (auto-numbers). Add the `App.tsx` render branch and the `Masthead.tsx` link button
(grounding area 3 §3). The screen renders audit reports + a breach feed + ack buttons. **Commit**
`feat(cockpit-ui): SYSTEM AUDIT screen + masthead wiring`.

### Task 26: Rebuild the committed Vite static

**Files:** `cockpit-ui/` build output under `src/swing_screener/cockpit/static/`.

**Steps:** `cd cockpit-ui && npm run build` (or the repo's build script), then `tsc`/typecheck must be
clean. Confirm only asset-hash + intended files changed; the eol=lf pin holds (per memory — Vite can
leak CRLF). **Commit** `build(cockpit): rebuild static with Coach panels + SYSTEM AUDIT`.

---

## Group H — Deployment

### Task 27: Azure jobs for coach_run + audit_run

**Files:** Modify `infra/modules/jobs.bicep` (add two `jobSpecs` entries + per-surface cap params/env).

**Steps:** Add `journal-coach` (`gateEnv:[]`, hourly cron — drains drafts + does the rollup) and
`journal-audit` (`RUN_IF_ET_HOUR=16`, weekly cron `'30 20,21 * * 6'` + a daily breach variant or a
daily cron) per grounding area 4 §3. Add `param coachMaxUsd string='1.00'` / `auditMaxUsd` and the
`SWING_COACH_MAX_USD`/`SWING_AUDIT_MAX_USD` + `SWING_COACH_ENABLED`/`SWING_AUDIT_ENABLED` env entries
(bicep default caps must be real numbers, never empty). No unit test; validate `az bicep build` if
available, else careful review. **Commit** `feat(infra): coach_run + audit_run ACA jobs (default-off, capped)`.

---

## Group I — Finish

### Task 28: Full green + type-check + finish the branch

**Steps:**
1. `.../python.exe -m pytest -q` from the worktree — full suite green.
2. Type-check the CI way: `MYPYPATH=src .../python.exe -m mypy src/swing_screener --no-incremental`
   from the worktree (bare `mypy` checks main's src — do not trust it). Fix pandas/None-guard typing.
3. `ruff check` clean; single Alembic head (`alembic heads` shows one).
4. Announce: "I'm using the finishing-a-development-branch skill to complete this work." →
   **REQUIRED SUB-SKILL:** superpowers:finishing-a-development-branch (present options; do NOT push/PR
   without Oliver's explicit go-ahead).

---

## Execution notes

- **Batch order:** Group 0 → A are strictly serial (foundation). B/C (Coach) and D (Auditor) are
  independent of each other and can interleave. E is tiny. F depends on B–E. G depends on F. H/I last.
- **Screenshot-verify** the two cockpit surfaces before finishing (claude-in-chrome CDP capture — the
  in-app browser screenshot hangs on the cockpit's persistent SSE).
- Keep every LLM path default-off in tests (inject a fake client); never hit the real API in CI.

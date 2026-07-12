# Journal v2 — The Two Coaches — Design

**Date:** 2026-07-12
**Status:** design approved section-by-section (2026-07-12); hardened by a 6-lens adversarial
review. Builds on Journal v1 (PR #116, on `main`).
**Supersedes:** the single **Session Review agent** in
[2026-07-11-journal-layer-design.md](2026-07-11-journal-layer-design.md) §"Journal v2 — the AI
half". That agent was one voice "aimed at BOTH the human's discretionary behavior and the
*system's* behavior." Per the owner's directive (2026-07-12) it is split into **two distinct
surfaces**: a **Personal Trade Coach** over his own real trades and an independent **System
Behavior Auditor** over the agents' trades. Everything else in the v1 doc's "AI half" (auto-tagger,
weaknesses profile, per-trade charts, Edge Score) is re-homed under the correct surface below.

## The one idea

Two review surfaces that **must never share data, prompt, or voice.** The Coach reviews *Oliver's*
decisions; the Auditor reviews *the machine's* conduct.

Their separation is partly physical and partly enforced. The Coach's real-trade rows live in
tables the machine never writes (`Trade`; `OptionPaperTrade` account `robinhood`). But **three
tables are shared and separated only by a `WHERE` filter** — `ExitEvent`, the journal overlay
(`journal_trade_tags` / `journal_notes` / `journal_theses`), and `OptionPaperTrade` (which holds
both the personal `robinhood` book and the machine `options-lab` book). For those, distinctness is
a discriminator we must apply *and test*, not a property we get for free (§Boundary spells out each
guard). So the honest framing is: **separate where the schema allows, filtered-and-tested where it
doesn't.**

Both surfaces obey the firewall the reflection engine uses: **code owns every number, the LLM owns
only the prose.** A deterministic grader computes and *persists* the facts first; the displayed
scorecard is rendered from those persisted facts in a code-owned region; the LLM's narrative is
advisory prose alongside them. If the LLM is off or fails, the scorecard still ships (§Persistence).

## Why two, not one

An analyst that grades its own machine has a conflict of interest. The suite's LLM analyst is a
*learning participant* in the machine's own decisions (North Star #9) — it nudges conviction, and
its calls are logged and scored (`AnalystCall`, [models.py:224](../../src/swing_screener/db/models.py)).
Letting that same voice also *audit* the machine's conduct would let the graded grade itself. So
the Auditor is a **separate grader, a separate prompt, and a deliberately separate voice** whose
context builder must not import the analyst's (asserted in tests, §Risks). The Coach, by contrast,
is pure decision-support *for the human* — squarely inside North Star #9's carve-out.

## The boundary

The manual/agent split is drawn in the data, but two categories behave differently.

**Category 1 — physically separate tables (no filter needed):**

| Surface | Tables |
|---|---|
| **Coach A — Personal** | `Trade` (whole table — [models.py:67](../../src/swing_screener/db/models.py), "Real / manually entered trades"; **no `account` column** because the entire table *is* the manual equity book) |
| **Auditor B — System** | `PaperTrade` ([models.py:94](../../src/swing_screener/db/models.py)), `ExecutionLog` ([models.py:259](../../src/swing_screener/db/models.py)), `AnalystCall`, `ReversalFunnel` ([models.py:365](../../src/swing_screener/db/models.py)) |

**Category 2 — shared tables, separated by a mandatory `WHERE` guard + a regression test:**

| Shared table | Coach guard | Auditor guard |
|---|---|---|
| `ExitEvent` (models.py:191) | `is_paper=False AND reason="manual_close"` | `is_paper=True`. **Must NOT scope by `account`** — a manual close is written `account="research"`, `is_paper=False` (`close_trade_with_event` default, [repo.py:521](../../src/swing_screener/db/repo.py)), so an `account`-scoped Auditor query would sweep the human's close in. `is_paper` is the real discriminator; `trade_id` resolves to `Trade` when `is_paper=False` else `PaperTrade` (overlapping id spaces). |
| overlay: `journal_trade_tags` / `journal_notes` / `journal_theses` (FK-less, keyed `(trade_id, book)`, [models.py:404](../../src/swing_screener/db/models.py)) | `book ∈ {manual_equity, robinhood}` | machine books only |
| `OptionPaperTrade` (models.py:538) | `account="robinhood"` | — (the Auditor reads `paper_trades` only, **never** `option_paper_trades`) |

**Required test:** the Auditor's context builder must return **zero** rows whose `reason="manual_close"`
or `book/account ∈ {manual_equity, robinhood}`; the Coach's must return zero machine-book rows.

**False friend — do not confuse.** `ExecutionLog.account == "manual"`
([execution.py:86](../../src/swing_screener/pipeline/execution.py)) is the **agent's manual-*mode*
order ticket** (a "recorded" ticket for a human to go place), **not** the `trades` table. The Coach
reads the `trades` table; the Auditor reads `ExecutionLog`. They share the word "manual" and nothing
else.

**Gray zone — excluded from BOTH surfaces in v2.** `OptionPaperTrade` account `options-lab` is a
genuine Oliver *decision* (he grades the 12-point checklist and takes/skips a setup) but books a
**simulated** fill. The owner scoped the Coach to "not paper trades," so it is out of the Coach; and
the Auditor reads `paper_trades` only, so it never sweeps `options-lab` in as "machine conduct."
Its checklist-discipline could feed the Weaknesses Profile later; deferred (§Scope).

## Surface A — the Personal Trade Coach

**Data scope.** Real money only: the `Trade` table (`book="manual_equity"`) + the `robinhood`
options book, plus the human's own `source="human"` annotations. Nothing the machine booked.

**The grader (code owns the numbers).** A pure function stamps the hard facts before any prose,
from data these tables actually hold:

- *Equity (`Trade`):* realized R computed `(exit_price − entry_price)/(entry_price − stop)`, with
  the close endpoint's guard carried over — **`risk ≤ 0 → R is null`** (mirrors
  [trades.py:248](../../src/swing_screener/cockpit/routers/trades.py)); result vs target (did it
  reach target / stop / neither, via `exit_reason`); stop and target honored vs. moved; hold time
  (`exit_date − entry_date`); the `override` deviation; `emotional_state`.
- *Options (`robinhood`):* **premium P&L in $ (never R)**, hold time, `exit_reason`. The 12-point
  A+ grade is **not** available here — imported episodes are booked `setup_id=None`
  ([broker_import.py:359](../../src/swing_screener/options/broker_import.py)), so no checklist grade
  exists; it appears only if the deferred options-lab feed is added.

*Not in the fact set:* **MAE/MFE excursion.** The excursion math reads `PaperTrade.low_water/
high_water` ([excursions.py](../../src/swing_screener/journal/excursions.py)); the `Trade` table has
no water marks and v2 adds none. Computing excursion for manual trades needs a hold-period
bar-window backfill — named as a **deferred dependency** (§Scope), not a v2 fact.

**Rhythm 1 — on close (per trade), async.** When Oliver closes a manual equity trade,
`close_trade_action` ([trades.py:236](../../src/swing_screener/cockpit/routers/trades.py)) writes
`ExitEvent(reason="manual_close")` **and synchronously stamps the deterministic `journal_reviews`
facts row**, then **enqueues** the LLM draft for an async worker (the `analysis_requests`-drained-by-
an-Azure-job pattern) so the HTTP close stays instant and the Anthropic key + spend capture stay
server-side, not in the local desktop process. The draft — *"you reached target but had moved your
stop up twice; realized 1.4R on a planned 2R"* — lands as a `journal_reviews` row Oliver can edit or
accept. It's a **draft for him, not a verdict over him.**

**Rhythm 2 — weekly rollup.** A weekly job (§Deployment) aggregates the week's closed manual trades
+ accumulated flags into a synthesis and refreshes the **Weaknesses Profile** — a compact,
staleness-stamped living list (*"moves stops under heat", "exits winners early"*) that feeds forward
as context into future reviews. Wears an explicit **thin-data label** until the corpus is real
(§Risks).

**Robinhood has no close event.** Imported episodes are bulk-inserted already-closed
([broker_import.py:336](../../src/swing_screener/options/broker_import.py)); no `ExitEvent` fires.
So the robinhood trigger is **on import**: stamp deterministic facts only for newly-committed
`import_key`s, hold `needs_review` episodes out until confirmed, and absorb the prose into the
weekly rollup — never a per-episode Opus draft (a first history import would otherwise fire hundreds
of calls at once). If per-trade robinhood prose is ever wanted, it runs as a one-time chunked
backfill under a hard import-batch USD cap.

**Auto-tagger with a real confirm gate.** Deterministic rules fire first — `override` present →
`deviation`; result short of target with a favorable exit_reason → `cut-winner-early`; stop moved →
`moved-stop`. The LLM handles only fuzzy cases. **Proposed tags do NOT land in `journal_trade_tags`**
— that table has no pending state and `mistake_cost` counts a row as a live confession the instant
it exists ([mistakes.py:27](../../src/swing_screener/journal/mistakes.py)). Proposals are parked in
the review's `facts_json`; only **on Oliver's confirm** is the `source="analyst"` tag row written to
the overlay.

**`emotional_state`.** A new nullable column on `Trade`, captured **at entry and at close** (exit-
decision emotion is often the more diagnostic for a coach). Requires wiring, not just the column:
`TradeCreate` + the close body accept it, `add_trade`/close persist it, and `LogTradeForm.tsx` /
the close form gain a free-text-with-suggestions field. Design v1 Principle 5: emotional labels live
*only* on discretionary actions, never on machine entries.

## Surface B — the System Behavior Auditor

**Independence.** Reuses the reflection engine's grader/author seam
([reflect.py:179](../../src/swing_screener/pipeline/reflect.py) `grade()` is a *pure* deterministic
grader; [reflect.py:480](../../src/swing_screener/pipeline/reflect.py) `analyst_calibration()` already
scores the machine's own conviction nudges) — pointed at **conduct, not edge.** `reflect.py` asks
*"does this strategy make money."* The Auditor asks *"did the machine follow its own rules, and did
anything drift out of spec."* Separate grader, prompt, and voice; its context builder must not
import the analyst's.

**Full oversight = two deterministic check families** (computed in code before any prose):

- **Compliance.** Risk-cap adherence — `ExecutionLog` per-day notional/loss sums vs. the mandate
  (the "SOURCE for the hard-limit sums", [models.py:265](../../src/swing_screener/db/models.py));
  **rejected/clamped order rate** (`ExecutionLog.status ∈ {rejected_live, rejected, skipped}`);
  **config-override churn** (`proposals.py` approve/withdraw; e.g. a `premium_only` override of
  `confirmed_only`), with attention to the v1 doc's named smell, **config-change-after-drawdown**;
  disarm events (see below). **`Trade.override` is Coach-only and off-limits to the Auditor** — there
  is no override column on any machine book, so the Auditor must never reach for it.
- **Anomalies.** Surfacing drought and `would_surface` leaks (`ReversalFunnel`
  detected→confirmed→fresh→actionable→surfaced); analyst-calibration drift (`AnalystCall`
  baseline-vs-final conviction and `nudge_vs_baseline_r` — *is the nudging disciplined against
  realized outcomes*); integrity checks (duplicate idempotency keys; `ExitEvent`/position mismatches
  — the known `max(PaperTrade.id)` trap).

**Disarm needs a persisted event (Scope prerequisite).** `POST /api/disarm`
([safety.py:87](../../src/swing_screener/cockpit/routers/safety.py)) cancels/re-submits orders and
bumps a nonce but **persists nothing** (log lines only). So auditing "unexpected disarm" requires
first adding a persisted disarm-event write (an `ExecutionLog`-style row or a small table) at that
endpoint — a v2 prerequisite, else disarm is dropped from the audited set.

**Cadence + how a breach reaches Oliver.** The Auditor runs as a **weekly ACA job** (§Deployment).
There is **no always-on process**, so "immediately" is honest only to the granularity that a job
runs: a hard breach (a cap exceeded → an `ExecutionLog` `skipped`/clamp row; an unexpected disarm)
surfaces on **the next pipeline run after the breach (daily)**, either written inline at clamp time
or caught by a daily scan of `ExecutionLog`, and is delivered by the existing notify path (ACS
email) — not by a fictitious real-time monitor.

**Read-only.** It **never** moves money, changes config, or disarms. It surfaces findings for
Oliver's attention and *he* acts (North Star #1/#3). Same in-row firewall: Opus narrates, code
stamps every figure.

## Shared substrate

**Spend gating.** LLM spend is *enforced* by an accumulator-and-ceiling like `notify/run.py`'s deep-
analysis path (`spend[0]` accumulates `est_cost`, `over_ceiling = max_usd is not None and spend[0]
>= max_usd`, [run.py:559](../../src/swing_screener/notify/run.py)) — **not** by `analyst.py`'s
`_spend`, which is read-only telemetry that `SELECT`s `AnalystCall` for the cockpit gate display.
Each surface is **default-off** with its **own** ceiling (`SWING_COACH_MAX_USD`,
`SWING_AUDIT_MAX_USD`), fail-safe *no cap on garbage config, never an accidental cap*. And the
telemetry `_spend` window must be widened to **UNION** `AnalystCall` + `journal_reviews` +
`system_audits` `est_cost_usd`, or the safety gate silently under-reports true suite spend once these
surfaces run.

**Persistence — code owns the numbers, in-row.** Unlike the edge files (git-tracked because they
*gate promotion*), these advisory surfaces are **DB tables**. The firewall is enforced, not just
column-split: deterministic `facts_json` is authoritative and written first; the **displayed
scorecard is rendered from `facts_json` in a code-owned region** (the analogue of `reflect.py`'s
`_restitch_calibration`, [reflect.py:443](../../src/swing_screener/pipeline/reflect.py)); the
`narrative` column is **advisory prose** written second and fail-safe (null → template). Numeric
claims embedded in the narrative are either forbidden or reconciled against `facts_json` before
display — so an LLM that writes "0.8R" when the facts say "0.5R" cannot put a fabricated figure in
front of the user.

**Events, not mutations.** The per-trade review hangs off the close *event* —
`ExitEvent(reason="manual_close")` for equities (robinhood fires on import, above) — never
`max(PaperTrade.id)` (v1 Principle 2). Nothing is recomputed by mutating a row.

**Never pooled.** Personal R and options premium never sum into one number; the Auditor's machine
books never touch the personal tables. Every derived figure stays unit-honest, book-scoped
(**North Star #2**), and carries a `generated_at` + covered-window **staleness stamp**
([ARCHITECTURE.md](../ARCHITECTURE.md) layer-4 contract — staleness is not a numbered North Star
principle).

## Data model (new, chains off Alembic head `a4e7c1b9f2d6`)

- **`journal_reviews`** — Coach A artifacts. `id`, `kind (trade_close | weekly_rollup)`, `book`,
  `trade_id` (null for rollups), `covered_from` / `covered_to` (rollup window), `facts_json`
  (authoritative scorecard **incl. parked tag proposals**), `narrative` (nullable LLM prose),
  `human_edit` (nullable), `source`, `model`, usage cols (`input_tokens`, `output_tokens`,
  `est_cost_usd`), `generated_at`. **Unique:** `(kind, book, trade_id)` for `trade_close`,
  `(kind, book, covered_from, covered_to)` for rollups (re-run idempotency).
- **`system_audits`** — Auditor B artifacts. `id`, `kind (weekly | breach)`, `period_from` /
  `period_to`, `findings_json` (deterministic: cap breaches, rejected/clamped rate, config-churn,
  disarm events, drought stats, calibration drift), `severity`, `narrative` (nullable), `model`,
  usage cols, `acknowledged_by_human` (bool), `generated_at`. **Unique:** `(kind, period_from,
  period_to)` for weekly, plus a `(period, breach_identity)` dedup key for breach rows.
- **`weaknesses_profiles`** — living distillation. `id`, `scope`, `items_json`
  (`[{weakness, evidence: [{book, trade_id}], first_seen, last_seen}]` — **`(book, trade_id)` pairs**,
  since Coach id spaces overlap across `Trade` and `OptionPaperTrade`), `covered_window`, `model`,
  `generated_at`. Append-only; latest row is current.
- **`trades.emotional_state`** — new nullable `String` column on the `Trade` model.
- **`disarm_events`** (or an `ExecutionLog` row) — persisted at `POST /api/disarm` (prerequisite).
- **Read-model change** — `record.py` gains a `Trade` producer (`module="swing"`,
  `book="manual_equity"`, computed R with the `risk ≤ 0 → null` guard) and a `robinhood` producer
  (`module="gex"`, `$` unit). The `TradeRecord.r: float|None` field
  ([record.py:61](../../src/swing_screener/journal/record.py), documented as R) is **generalized to
  `result` with a `unit` tag** before the `$` producer is added; the `OptionPaperTrade`
  `datetime → date` coercion for `opened/closed` is specified. Joins the existing overlay by
  `(trade_id, book)` — no FK crosses the boundary ([models.py:404](../../src/swing_screener/db/models.py)).

## Deployment & scheduling

The suite has **no in-app scheduler**; scheduled DB-writing work runs as **Azure Container Apps
Jobs** (`infra/modules/jobs.bicep`: cron + eastern-time gate + a CLI entrypoint; `ANTHROPIC_API_KEY`
+ `SWING_DB_URL` flow via `commonEnv`). Reflection (`reflect.yml`) is a *different* thing — a
GitHub-Actions cron that writes git edge files and opens a PR, does **no** DB writes, and **cannot
reach** the prod Azure SQL DB. So "mirror the reflection cadence" is a category error and is dropped.

v2 adds two CLI entrypoints — **`journal.coach_run`** (weekly rollup + profile refresh; also drains
the on-close draft queue) and **`journal.audit_run`** (weekly sweep + daily breach scan) — wired as
`jobs.bicep` entries with concrete crons and the ET gate, or folded into existing daily/weekly jobs.
The on-close *facts* row is written synchronously in the cockpit; only its LLM draft rides the queue
a job drains.

## Cockpit surfaces

- **Coach → inside the Journal screen.** The Coach's per-trade reviews, weekly rollup, and Weaknesses
  Profile render inside the existing **JOURNAL** screen. The book selector gains `manual_equity` and
  `robinhood`; the existing research/paper/live are **machine** shadow-grid books (Auditor-side), so
  **the Coach's review/rollup/profile panels render ONLY over the personal books, never over a
  machine book** (the Coach voice is never applied to machine-book views). Whether the machine books
  are removed from JOURNAL entirely (relocated to SYSTEM AUDIT) or retained read-only is an
  implementation choice; either way the Coach panels are personal-book-gated.
- **Auditor → its own screen.** A new digitless masthead-link screen **SYSTEM AUDIT** — operational
  oversight of the machine is a different concern and a different voice, so it gets its own door.
- **Mount cost is mechanical:** the same 3-touch pattern the Journal/GEX tabs already followed —
  a digitless registry row ([screens.ts:46-48](../../cockpit-ui/src/lib/screens.ts), where `journal`
  and `gexlab` already live) → masthead link ([Masthead.tsx:198](../../cockpit-ui/src/components/Masthead.tsx))
  → `App.tsx` state branch. No new nav machinery.

## Scope

**In v2:**
1. Re-point the read-model at the manual books (`Trade` + `robinhood`), generalize `TradeRecord`
   (`r → result` + unit), surface both books in the Journal selector — the prerequisite.
2. Coach A: on-close per-trade review (sync facts + async draft) + weekly rollup + Weaknesses
   Profile + auto-tagger (with the real confirm gate) + `emotional_state` (entry + close).
3. Auditor B: weekly sweep + daily breach scan; **persisted disarm-event** prerequisite.
4. Shared: the new tables + migration; per-surface spend caps + widened telemetry window; the
   `journal.coach_run` / `journal.audit_run` ACA jobs; the SYSTEM AUDIT screen + Journal Coach panels.

**Deferred (YAGNI / data-gated):**
- **MAE/MFE for manual trades** — needs a hold-period bar-window backfill (or `Trade` water-mark
  instrumentation); not shipped in v2.
- **Per-trade charts** with exit markers in a trade-detail view — `render.py` is PDF/email-only and
  draws no exit marker; a rendering feature orthogonal to the coaching intelligence.
- **Edge Score** (composite radar) — needs a real corpus of closed manual trades; data-gated.
- **Options-lab checklist discipline → Weaknesses Profile** — once there's lab history.

## Guardrails (charter compliance)

| Principle | How v2 honors it |
|---|---|
| **#1** evidence gates promotion/execution | Neither surface changes config or moves money; the Auditor is read-only; the Coach is advice. |
| **#2** honest about uncertainty; books never pool | Personal R and robinhood premium stay separate; machine and personal books never mix; thin-data labels on the profile. |
| **#3** human gate, reversible | Every artifact is a draft/finding Oliver accepts or acts on; nothing auto-executes. |
| **#4** deterministic levels are ground truth | The LLM annotates and advises; it never moves a level; the grader owns every number. |
| **#6** remove discretion where emotion leaks in | The Coach *names* discretionary overrides, early exits, and `emotional_state` so the leak is visible and correctable. |
| **#7** measure only what I'd actually trade | The Auditor's `would_surface`-leak / drought check polices exactly this on the machine side. |
| **#9** analyst is a learning participant | The Coach has genuine judgment in decision-support; its accountability half (every call earns a track record) maps to the **Weaknesses-Profile feed-forward** — reviews accumulate into an evidence-stamped profile revised as trades close. The Coach makes no scored *conviction* call, so `AnalystCall`-style per-call scoring doesn't apply. The **Auditor** is walled off from the analyst it audits to avoid self-grading. |
| staleness stamps (ARCHITECTURE layer-4) | Reviews, audits, and the profile all carry `generated_at` + covered window. |

## Risks / open questions

- **Thin personal corpus.** Oliver trades swing/discretionary, not daily — per-trade reviews are
  low-volume (fine for spend), but the Weaknesses Profile reads as noise until enough trades close.
  It must wear an explicit thin-data label; Edge Score stays deferred until a corpus exists.
- **Read-model divergence.** A `Trade` producer with a different shape (computed R, no `account`, no
  water marks) risks drift from the `PaperTrade` path; both producers must be golden-tested against
  the same annotation-join contract.
- **Auditor independence is a discipline, not a wall.** Nothing technical stops a future edit from
  feeding the analyst's context into the Auditor prompt; the separation is asserted in a test (the
  Auditor's context builder must not import the analyst's) and in the §Boundary zero-leak tests.
- **"Immediately" is job-granular.** Breach alerts are as timely as the daily job, not real-time;
  accepted for v2. A true real-time monitor is out of scope.

## Explicitly not building

Chat-first coaching UX; auto-learned rules from top-P&L trades (the overfitting trap); an Auditor
that can act (disarm/patch/roll-back) rather than report; a single merged review surface; per-trade
dollar-first headline stats; emotional labels on machine entries; a real-time breach monitor.

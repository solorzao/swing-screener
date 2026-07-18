# Audit Remediation & Cost Optimization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the 2026-07-17 deep audit's verified findings — live-money safety first, then the approved API-side cost optimization (~50–70% off the ~$55–75/mo Opus spend), then correctness/infra hardening and streamlining.

**Architecture:** Thirteen independent PR-sized phases, ordered by risk. Each phase is a coherent code area so PRs stay reviewable. Phase E deliberately consolidates the 4× copy-pasted Opus analyst scaffold *first*, then applies every cost lever inside the one helper. No behavior changes outside the named fixes.

**Tech Stack:** Python 3.12 / SQLAlchemy 2 / Alembic / FastAPI / React (cockpit-ui) / Bicep (Azure Container Apps jobs) / pytest.

**Source of truth for findings:** the audit report artifact (105 verified findings) — https://claude.ai/code/artifact/c1bf8653-746f-4ba2-96c8-dc6e694da2a8. Finding titles quoted below match it.

**Repo conventions that apply to every phase:**
- Run `ruff check src tests` and `MYPYPATH=src mypy src/swing_screener` before pushing (CI runs both; bare `mypy` in a worktree checks main's code, not yours).
- SQL Server portability: filter booleans with `== True` / `== False`, never `.is_(True)`. No NaN floats to TDS. Test both sqlite and the mssql-shaped cases where the finding is mssql-specific.
- Bicep template defaults MUST match intended prod state (2026-07-01 redeploy-disarm lesson).
- One PR per phase; checkpoint-commit early and often on the phase branch.

**Open decisions (defaults chosen; flag in PR if changing):**
1. `deep_analysis_top_n` stays 5-per-list (10 daily calls). The other levers already cut ~50–70%; halving coverage is a product call Oliver can make later via env (`SWING_DEEP_ANALYSIS_TOP_N`).
2. Coach/Auditor prose moves to `claude-haiku-4-5` (bounded 600-token template-grade output). Escape hatch: env override stays supported.
3. `option_review_facts` (robinhood coach reviews never produced): **kept, wiring deferred** to a follow-up feature decision — Phase L only annotates it, does not delete.
4. Trade-thesis writer (`add_thesis`, dead): delete the dead function + repo method; keep the `journal_theses` table/migration (harmless, avoids a destructive migration).

---

## Execution state & resume protocol (read this first)

Sessions WILL get interrupted (usage caps, restarts). This plan is built to survive that; follow these rules and no work is ever lost or re-derived.

**The Progress Ledger below is the single source of truth for what is done.** Session task lists and chat context do not survive; this file does.

**Rules while executing:**
1. **Commit after every green test** — each task's steps already end in a commit. Never batch multiple tasks into one commit.
2. **Tick the ledger checkbox in the SAME commit** as the task's final commit (`git add docs/plans/... src/... tests/...`). A ticked box == that task's code is committed and its tests pass.
3. **If you must stop mid-task** (usage warning, end of session): commit whatever exists as `wip(<task-id>): <one line on exact stopping point>` and add a `> WIP:` note under the task's checkbox describing the next step. A `wip:` commit is always safe to push to the phase branch — CI failing on a phase branch is fine.
4. **Never leave uncommitted edits at the end of a turn** in a shared worktree (the 2026-07 stash-pop incident: an aborted git operation wiped parallel uncommitted work).

**Cold-start resume (fresh session, zero context):**
1. `git fetch origin main` and read this file on the phase branch (or create the branch from latest main if it doesn't exist).
2. Find the phase's first unticked checkbox. Check `git log --oneline -10` for a trailing `wip(<task-id>)` commit — if present, read its `> WIP:` note and finish that task first.
3. Run that phase's test suite before writing anything (`pytest tests/<area> -q`) to confirm the baseline is green.
4. Continue task-by-task per superpowers:executing-plans.

**Phase independence:** every phase is its own branch off latest main with no cross-phase dependency (only exception: E2–E6 need E1's helper — they are inside one branch anyway). An interruption in one phase never blocks another; phases can also run in parallel worktrees.

### Progress Ledger

**Phase A — `fix/live-money-safety`**
- [ ] A1 account-scoped same-day delete + regression tests
- [ ] A2 ExecutionLog idempotency status upgrade + adapter load-before-act guards
- [ ] A3 paginated `list_open_orders`

**Phase B — `fix/queue-claim-datetime`**
- [ ] B1 whole-second claim tokens in both claim functions
- [ ] B1-verify (post-deploy) one on-demand request processes end-to-end in prod

**Phase C — `fix/nan-hardening`**
- [ ] C1 `_download` dropna + HA recovery test
- [ ] C2 NaN guards: detect ATR truthiness / `latest_close` / `avg_dollar_volume` / `_spot_from_ticker`
- [ ] C3 NaN fundamentals rendered into analyst prompts

**Phase D — `fix/auditor-compliance-math`**
- [x] D1 cap sums filtered to counting statuses, per account, `n_clamps` surfaced
- [ ] D2 `max_daily_loss` graded in realized R
- [ ] D3 breach-scan window / empty-narrative rows / `would_surface_leaks` overflow

**Phase E — `feat/analyst-cost-optimization`** (strict order E1 → E6)
- [ ] E1 `_analyst_call` consolidation (behavior-preserving; both analysis test suites green unchanged)
- [ ] E2 `web_search_20260209` + legacy-retry in `_create_message`
- [ ] E3a `_MODEL_PRICES` additions + unknown-model fail-safe warning
- [ ] E3b billed-but-failed usage reaches the spend accumulator
- [ ] E3c `analyze_ticker_deep` usage capture + `analysis_requests.est_cost_usd` migration
- [ ] E4 `daily_picks` per-ticker dedup (+ cockpit picks mirror)
- [ ] E5 bicep reasoning default `medium` + coach/audit on `claude-haiku-4-5` (single model constant per worker)
- [ ] E6 Market Weather env switch + spend visibility
- [ ] E-verify (post-deploy, ~1 week) `analyst_calls.input_tokens` well below the 400k/run baseline

**Phase F — `fix/digest-cooldown-cadence`**
- [ ] F1 per-kind cooldown + dropped-picks log line

**Phase G — `fix/infra-cd-hardening`**
- [ ] G1 `notify.run` `_resolve_db_url` + mssql-gated migrate
- [ ] G2 `market_run` mssql-gated migrate
- [ ] G3 `imageTag` default `latest` (3 files)
- [ ] G4 CD single-alembic-head guard
- [ ] G5 CI push-trigger filter
- [ ] G6 SQL token out of `GITHUB_ENV`
- [ ] G7 cockpit DB write grants + runbook
- [ ] G8 stale bicep secret docs

**Phase H — `fix/fetch-cache-poisoning`**
- [ ] H1 pre-close truncated frame not written to the day cache
- [ ] H2 GEX settle: eod_flat only on complete sessions; pre-16:00 refusal without `--force`
- [ ] H3 GEX `--cache-dir` wired through

**Phase I — `perf/hot-path-indexes`**
- [ ] I1 one migration: `ix_signals_run_date`, `ix_paper_trades_status_account`, `ix_exit_events_created_date`, filtered-unique `import_key`

**Phase J — `fix/cockpit-api-degrade`**
- [ ] J1 per-row degrade on corrupt JSON rows
- [ ] J2 weaknesses `items_json` shape
- [ ] J3 heartbeats sidecar stat guard
- [ ] J4 GEX POST 503 posture
- [ ] J5 atomic manual close
- [ ] J6 `loss_r` gauge research-grid scoping
- [ ] J7 `spend_rows_since` SQL cutoff

**Phase K — `fix/cockpit-ui-polish`**
- [ ] K1 surfaced errors (tag-confirm + acknowledge x2)
- [ ] K2 CSV input reset
- [ ] K3 fmt fixes (fmtResult / null realized_r / null max_drawdown)
- [ ] K4 keyboard-reachable open-book scroll region
- [ ] K5 dead CSS refs + dead null-check + time/date helper consolidation (brace-count after every CSS edit)

**Phase L — `chore/dead-code-sweep`** (one commit per deletion; `git grep` before each)
- [ ] L1 `cancel_all_orders` (Protocol + both impls)
- [ ] L2 `latest_signals` + `update_trade`
- [ ] L3 `fetch_universe` + `fetch_vix`
- [ ] L4 `rank_bucket` / `score_bucket` wrappers
- [ ] L5 `total_unrealized_pl`
- [ ] L6 `analyze_ticker` wrapper (rewrite its 5 test call sites)
- [ ] L7 `discipline.py` ImportError fallback
- [ ] L8 `add_thesis` writer (table stays)
- [ ] L9 `conviction_sizing` / `conviction_weight_*` knobs
- [ ] L10 stale `storage/__init__.py` facade
- [ ] L11 `option_review_facts` NOTE annotation (no delete)

**Phase M — `chore/replay-and-test-structure`**
- [ ] M1 shared `load_replay_corpus` across the 18 replay scripts (exclusion set decided + documented)
- [ ] M2 `tests/cockpit/test_api.py` split per router (moves only)
- [ ] M3 reflect.py drift pair
- [ ] M4 notify low items (blob logging / dedup key / side-aware instruction / ACS guard / single autonomy_gate)

---

## Phase A — Live-money safety (P0) — branch `fix/live-money-safety`

### Task A1: Account-scope the same-day idempotency delete

**Finding:** *Same-day screen re-run deletes live and paper account trades (unrecoverable for live)* — critical.

**Files:**
- Modify: `src/swing_screener/db/repo.py:95-97` (`delete_paper_trades_opened_on`)
- Test: `tests/db/test_trade_repo.py` (add), `tests/pipeline/test_run_live_reconcile.py` (add regression)

**Step 1: Write the failing tests**

```python
def test_delete_paper_trades_opened_on_only_touches_research(session):
    """A same-day re-run delete must never touch live/paper/manual rows."""
    for account in ("research", "live", "paper", "manual"):
        session.add(_mk_trade(account=account, opened_date=date(2026, 7, 17)))
    session.commit()
    repo.delete_paper_trades_opened_on(session, date(2026, 7, 17))
    remaining = {t.account for t in session.scalars(select(PaperTrade)).all()}
    assert remaining == {"live", "paper", "manual"}
```

And in `test_run_live_reconcile.py`: materialize a live fill via the existing test harness, call `run_screen` a second time for the same date, assert the `account="live"` PaperTrade row survives.

**Step 2: Run to verify both fail** — `pytest tests/db/test_trade_repo.py -k research -v` → FAIL (live row deleted).

**Step 3: Implement**

```python
def delete_paper_trades_opened_on(session: Session, opened_date: date) -> None:
    """Delete ONLY the research shadow grid's same-day rows (re-run idempotency).

    Live/paper/manual rows are NOT re-created by a screen re-run (their
    ExecutionLog is already terminal), so deleting them permanently loses real
    positions -- the 2026-07-17 audit's critical finding.
    """
    session.execute(delete(PaperTrade).where(
        PaperTrade.opened_date == opened_date,
        PaperTrade.account == "research",
    ))
    session.commit()
```

**Step 4: Run the two tests + the full `tests/db` and `tests/pipeline` suites** — PASS.

**Step 5: Commit** — `fix(db): scope same-day idempotency delete to the research account`

### Task A2: ExecutionLog idempotency must not freeze status

**Finding:** *Idempotency-key collision freezes ExecutionLog status* — high. A `skipped` row silently swallows a later `submitted_live`/`filled_paper` write for the same key.

**Files:**
- Modify: `src/swing_screener/db/repo.py:349-370` (`add_execution_log`)
- Modify: `src/swing_screener/pipeline/execution.py` (LiveAdapter + PaperAdapter submit paths — load-before-act guard)
- Test: `tests/pipeline/test_execution.py`

**Design:** two layers, matching the finding's suggested fix.
1. `add_execution_log`: on IntegrityError, if the existing row is in a **non-counting** status (`skipped`, `rejected`, `rejected_live`, `canceled`) and the attempted write is a **counting** status (`_LIMIT_COUNTING_STATUSES`), UPDATE the existing row's `status`/`detail` to the new values and log a warning; otherwise keep today's return-existing no-op.
2. Adapters: before acting, load the existing log row for the key; short-circuit if it is already terminal-success (prevents the PaperAdapter double-open and the LiveAdapter double-submit on replays).

**Step 1: Failing tests** (three):

```python
def test_add_execution_log_upgrades_skipped_to_submitted(session):
    first = repo.add_execution_log(session, **_log_fields(status="skipped"))
    second = repo.add_execution_log(session, **_log_fields(status="submitted_live"))
    assert second.id == first.id
    assert second.status == "submitted_live"      # upgraded, not swallowed

def test_add_execution_log_never_downgrades_counting_status(session):
    repo.add_execution_log(session, **_log_fields(status="filled_paper"))
    again = repo.add_execution_log(session, **_log_fields(status="skipped"))
    assert again.status == "filled_paper"          # terminal wins

def test_paper_adapter_no_double_open_after_skip_then_fill(session):
    # skip (clamp) -> limit frees -> resubmit fills -> third submit must short-circuit
    ...
    assert count_open_positions == 1
```

**Step 2: Run to verify FAIL.**

**Step 3: Implement** — in `add_execution_log`, replace the bare return-existing with:

```python
    except IntegrityError:
        session.rollback()
        key = fields["idempotency_key"]
        existing = session.scalars(
            select(ExecutionLog).where(ExecutionLog.idempotency_key == key)
        ).one()
        new_status = str(fields.get("status", ""))
        if (existing.status not in _LIMIT_COUNTING_STATUSES
                and new_status in _LIMIT_COUNTING_STATUSES):
            # A non-counting record (skipped/rejected) is being superseded by a
            # real submission on the same key: record the truth, loudly.
            log.warning("execution log %s upgraded %s -> %s on idempotency-key reuse",
                        key, existing.status, new_status)
            existing.status = new_status
            existing.detail = fields.get("detail", existing.detail)
            session.commit()
        return existing
```

(Module needs a `log = logging.getLogger(__name__)` if absent.) Then add the load-before-act guard in `LiveAdapter.submit` / `PaperAdapter.submit`: query the key's existing row first; if status is in `_LIMIT_COUNTING_STATUSES`, return the "already submitted" result without calling the broker.

**Step 4: Full `tests/pipeline` + `tests/db` — PASS.**

**Step 5: Commit** — `fix(execution): idempotency-key reuse upgrades skipped logs instead of swallowing real submissions`

### Task A3: Paginate Alpaca `list_open_orders`

**Finding:** *list_open_orders is unpaginated (Alpaca default limit=50), so disarm/stop-protection can silently miss orders* — low but same blast radius (disarm completeness).

**Files:** Modify `src/swing_screener/pipeline/broker.py` (`list_open_orders`); Test `tests/pipeline/test_broker.py`.

**Steps:** failing test with a fake HTTP layer returning 50-item pages → implement `until`/`after` cursor loop (Alpaca `GET /v2/orders?status=open&limit=500&after=<ts>`) until a short page → PASS → commit `fix(broker): paginate list_open_orders so disarm sees every open order`.

---

## Phase B — Queue claim fix (P0) — branch `fix/queue-claim-datetime`

### Task B1: Make the claim read-back round-trip exact

**Finding:** *Queue-claim read-back `started_at == now` can match zero rows on SQL Server (DATETIME 1/300s rounding)* — high. On-demand analysis and coach drafts never process on Azure SQL.

**Files:**
- Modify: `src/swing_screener/db/repo.py:698-717` (`claim_queued_requests`) and `:767-784` (`claim_queued_coach_drafts`)
- Test: `tests/db/test_analysis_queue.py`, `tests/journal/test_coach_queue.py`

**Fix shape:** truncate the claim token to whole seconds inside both functions — whole seconds are exactly representable in DATETIME, so the parameter the SELECT binds equals the stored value on every backend. (The alternative — DATETIME2 migration — is more invasive; truncation preserves the race-safety design as documented.)

**Step 1: Failing test** — simulate the mssql rounding: monkeypatch the session/type so the stored `started_at` loses microseconds (or assert directly that the claim functions stamp a microsecond-free datetime):

```python
def test_claim_token_has_no_microseconds(session):
    repo.create_analysis_request(...)
    claimed = repo.claim_queued_requests(
        session, now=datetime(2026, 7, 17, 12, 0, 0, 123456, tzinfo=UTC))
    assert claimed, "claim must survive DATETIME's 1/300s rounding"
    assert claimed[0].started_at.microsecond == 0
```

**Step 2: FAIL.**

**Step 3: Implement** — first line of both claim functions:

```python
    # DATETIME on SQL Server rounds to 1/300s; whole seconds round-trip exactly,
    # so the read-back equality below works on every backend (2026-07-17 audit).
    now = now.replace(microsecond=0)
```

**Step 4: PASS + full `tests/db` + `tests/journal`.**

**Step 5: Commit** — `fix(db): truncate queue-claim token to whole seconds so the read-back matches on SQL Server`

**Step 6 (verification, manual):** after deploy, queue one on-demand analysis request and confirm it processes (this path has been dead in prod — the fix is also the proof the audit was right).

---

## Phase C — NaN hardening (P0) — branch `fix/nan-hardening`

### Task C1: Drop NaN OHLC rows at the shared fetch seam

**Finding:** *A single NaN OHLC row permanently poisons the HA recursion* — high.

**Files:** Modify `src/swing_screener/data/fetch.py:53-60` (`_download`); Test `tests/data/test_fetch.py`, `tests/indicators/test_heiken_ashi.py`.

**Step 1: Failing tests**

```python
def test_download_drops_nan_ohlc_rows(monkeypatch):
    frame = _frame_with_nan_close(rows=30, nan_at=10)
    monkeypatch.setattr(fetch.yf, "download", lambda *a, **k: frame)
    out = fetch._download("TEST", "1d", "5y")
    assert not out[["open", "high", "low", "close"]].isna().any().any()

def test_heiken_ashi_recovers_after_nan_row():
    """Defense in depth: HA output must not be NaN forever after one NaN input."""
```

**Step 2: FAIL.** **Step 3:**

```python
    df = df.rename(columns=str.lower)
    # One NaN close poisons the HA open recursion for every subsequent bar
    # (silently killing all detectors for the ticker) -- drop incomplete rows
    # here so every consumer is covered (the PR #104 class, fixed at the seam).
    return df[_COLS].dropna(subset=["open", "high", "low", "close"])
```

**Step 4: PASS + full `tests/data` + `tests/indicators` + `tests/signals`.** **Step 5: Commit.**

### Task C2: NaN-truthiness guards in detect + quote paths

**Findings:** *NaN ATR passes truthiness guards in signals/detect.py*; *latest_close returns NaN instead of None*; *avg_dollar_volume can return NaN → TDS 8023*; *_spot_from_ticker passes NaN lastPrice through* (GEX).

**Files:** `src/swing_screener/signals/detect.py`, `src/swing_screener/data/quotes.py`, `src/swing_screener/data/fetch.py:120-125`, `src/swing_screener/options/chain.py` (or wherever `_spot_from_ticker` lives — grep first); tests alongside each.

**Pattern for all four:** failing test feeding NaN → guard with `math.isnan`/`pd.notna` returning the documented None/fallback → pass → one commit per file or one squashed `fix(data): NaN guards on quote/volume/spot paths`.

### Task C3: NaN fundamentals render as "nan" into analyst prompts

**Finding:** *NaN values from yfinance .info render as 'nan' into the analyst prompt* — the PR #104 sibling in `notify/` (context builder). Guard at the formatting site: skip or render `n/a` for non-finite numbers. Failing test → fix → commit.

---

## Phase D — Auditor math (P0-adjacent) — branch `fix/auditor-compliance-math`

### Task D1: Grade with the limit engine's own statuses, per account

**Finding:** *Auditor cap-breach math counts clamped/rejected orders the limit engine deliberately excludes* — high.

**Files:**
- Modify: `src/swing_screener/journal/audit_compliance.py:39-76`
- Modify: `src/swing_screener/db/repo.py` (export `_LIMIT_COUNTING_STATUSES` as `LIMIT_COUNTING_STATUSES`; keep the old name as an alias)
- Test: `tests/journal/test_audit_compliance.py`

**Step 1: Failing tests**

```python
def test_clamped_day_is_not_a_breach(session):
    """Fills up to the cap + one skipped clamp row must NOT report a breach."""
    _add_log(session, status="filled_paper", notional=900.0)
    _add_log(session, status="skipped", notional=500.0)   # the clamp record
    f = compliance_findings(session, period_from=D, period_to=D,
                            max_daily_notional=1000.0, max_daily_loss=None)
    assert f.cap_breaches == []
    assert f.n_total == 2                                  # reject_rate still sees all rows

def test_caps_grade_per_account(session):
    _add_log(session, account="paper", status="filled_paper", notional=800.0)
    _add_log(session, account="live", status="filled_live", notional=800.0)
    f = compliance_findings(..., max_daily_notional=1000.0, ...)
    assert f.cap_breaches == []                            # 800 < 1000 per account
```

**Step 2: FAIL.** **Step 3:** filter the cap-sum loop to `log.status in LIMIT_COUNTING_STATUSES`, key the sums by `(created_date, account)`, and report per-account breach rows (add `account` to the breach dict). Keep the unfiltered `logs` list for `n_total`/`reject_rate`. Add a separate `n_clamps` count (skipped rows) to `ComplianceFindings` — clamps are good conduct worth surfacing, not breaches.

**Step 4: PASS.** **Step 5: Commit.**

### Task D2: Grade `max_daily_loss` in the unit the mandate defines (realized R)

**Finding:** *max_daily_loss unit mismatch* — high. Execution treats it as an R breaker on realized loss; the auditor compares against summed entry risk dollars.

**Files:** `src/swing_screener/journal/audit_compliance.py`, `src/swing_screener/journal/audit_run.py:46,139`; tests.

**Fix:** replace the `risk > max_daily_loss` comparison with the day's summed **realized R** (reuse `repo.realized_r_on(session, run_date=day, account=account)`), breaching when `day_r <= -max_daily_loss` — the same predicate as `execution.py`. Keep the entry-risk sum in the findings dict but rename its key `risk_dollars` (informational). Failing test: a day at −3R with `max_daily_loss=2.0` breaches; a day with $500 entry risk and no realized loss does not.

### Task D3: Breach-scan window, empty-narrative rows, and `would_surface_leaks`

**Findings (3 medium):** breach scan is today-only (misses post-16:00 ET and weekends); breach rows render a narrative from EMPTY findings ("0 cap breach(es)…" on an alert row); `would_surface_leaks` counts routine top-5/sector-cap overflow as an anomaly (erodes the $0 dead-week gate).

**Files:** `src/swing_screener/journal/audit_run.py` (scan window: since last breach-scan run — persist/lookup the previous scan date, scan `(last_scan, today]`), the breach-row narrative call site (pass the actual findings that triggered the row), `src/swing_screener/journal/audit_signals.py` (or wherever `would_surface_leaks` lives — exclude picks dropped only by the top-N/sector cap from the leak count). One failing test per fix; three commits.

---

## Phase E — Cost optimization (approved) — branch `feat/analyst-cost-optimization`

> Order matters: E1 (consolidation) first, so E2–E4 are single-site changes.

### Task E1: Extract the shared `_analyst_call` helper

**Finding:** *Opus analyst call scaffold copy-pasted four times* (analysis.py:384-411, 523-555, 693-725, market_analysis.py:161-184).

**Files:**
- Modify: `src/swing_screener/notify/analysis.py` (new helper; three call sites rewritten over it)
- Modify: `src/swing_screener/notify/market_analysis.py` (fourth call site; drop the six underscore-private imports)
- Test: existing suites `tests/notify/test_analysis.py`, `tests/notify/test_market_analysis.py` must pass unchanged (behavior-preserving refactor — the fakes injected via the `client` seam keep working)

**Helper signature (returns text + citations + usage so E3 becomes trivial):**

```python
def _analyst_call(
    *, system: str, content: list[dict], client: anthropic.Anthropic | None,
    model: str, reasoning: str, max_searches: int, web_search: bool,
) -> tuple[str, list[tuple[str, str]], Usage | None]:
    """One place that owns client construction, kwargs assembly, the thinking/
    effort wiring, web-search tool config, _create_message, text+citation
    extraction, usage capture, and the empty-response check. Raises on failure;
    each analyst keeps its own fallback."""
    client = client or anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
    kwargs: dict = {
        "model": model,
        "max_tokens": _REASONING_MAX_TOKENS.get(reasoning, 16000),
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    effort = _REASONING_EFFORT.get(reasoning)
    if effort is not None:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": effort}
    if web_search:
        kwargs["tools"] = [{"type": _WEB_SEARCH_TOOL, "name": "web_search",
                            "max_uses": max_searches}]
    resp = _create_message(client, kwargs)
    usage = _capture_usage(resp, model)          # captured BEFORE the empty check (E3)
    text, sources = _extract_text_and_citations(resp)
    if not text.strip():
        raise EmptyAnalysisError(usage)          # carries usage to the caller's fallback
    return text, sources, usage
```

With `class EmptyAnalysisError(ValueError): def __init__(self, usage): ...`. Rewrite `analyze_signal_deep`, `analyze_ticker_deep`, `analyze_conviction`, and `analyze_market_deep` to call it; each keeps only prompt building, parsing, and its deterministic fallback.

**Steps:** run both existing test suites first (green baseline) → refactor one function at a time, running its tests after each → commit per function or one `refactor(notify): consolidate the 4x Opus analyst scaffold into _analyst_call`.

### Task E2: Upgrade the web-search tool (the biggest cost lever)

**Finding:** *All four web-search call sites use the legacy web_search_20250305* — search-result injection is the largest input-token component (~400k tok/run measured).

**Files:** `src/swing_screener/notify/analysis.py` (module constant), tests that assert tool wiring.

**Step 1: Failing test** — assert the fake client received `{"type": "web_search_20260209", ...}`.

**Step 2/3:** add `_WEB_SEARCH_TOOL = "web_search_20260209"` (used by E1's helper — one site now). Extend `_create_message`'s BadRequest retry to also strip/downgrade `tools` if the message names the tool type (mirror of the thinking-param retry, so an older `SWING_ANALYSIS_MODEL` still works):

```python
        if "web_search" in msg and "tools" in kwargs:
            legacy = dict(kwargs, tools=[{**t, "type": "web_search_20250305"}
                                         for t in kwargs["tools"]])
            return client.messages.create(**legacy)
```

**Step 4/5:** suite green → commit `feat(notify): web_search_20260209 with dynamic filtering on all analyst calls`.

**Post-deploy verification:** compare `analyst_calls.input_tokens` per run for a week against the ~400k baseline; expect a large drop. This is the number that proves the savings.

### Task E3: Cap-integrity — billed-but-failed calls, unknown models, on-demand metering

**Findings (3 medium):** billed-but-failed calls add $0 to the accumulator; unknown `analysis_model` prices to $0 (cap no-ops); `analyze_ticker_deep` discards usage.

**Files:** `src/swing_screener/notify/analysis.py`, `src/swing_screener/notify/run.py:493-530`, `src/swing_screener/notify/ondemand.py:123`, `src/swing_screener/db/repo.py` (`complete_analysis_request` gains `est_cost_usd`), `src/swing_screener/db/models.py` + one alembic migration (`analysis_requests.est_cost_usd` nullable float); tests.

**Sub-steps:**
1. `_MODEL_PRICES`: add `claude-fable-5 (10.0, 50.0)`, `claude-sonnet-5 (3.0, 15.0)`, `claude-opus-4-6 (5.0, 25.0)`, `claude-haiku-4-5 (1.0, 5.0)`. Unknown model → fall back to the opus price **and** `log.warning` once per process (fail-safe: cap overcounts rather than no-ops). Failing test: unknown model yields nonzero token cost + warning.
2. Fallback usage: `analyze_signal_deep`/`analyze_conviction` catch `EmptyAnalysisError` (and post-response parse failures) and attach the captured `usage` to the deterministic fallback result, so `_deep_one`'s `spend[0] += cost` charges billed failures. Failing test: fake client returns empty content → fallback fires → `spend` accumulates > 0.
3. `analyze_ticker_deep`: return a `TickerAnalysis` dataclass `(summary, analysis_text, is_deep, usage)`; `ondemand.process_one` persists `usage.est_cost_usd` via `complete_analysis_request`. Migration + failing test.

Commit per sub-step.

### Task E4: `daily_picks` per-ticker dedup (correctness + refunds double billing)

**Finding:** *daily_picks has no per-ticker dedup* — `src/swing_screener/notify/select.py:59`.

**Step 1: Failing test** — two Signal rows for the same ticker (1d + 1wk) ranked 1–2, plus 5 distinct tickers below: `daily_picks(top_n=5)` must return 5 distinct tickers with the dup's best-ranked row first.

**Step 3:** over-fetch (no LIMIT or `top_n * 4`), iterate in rank order keeping first row per ticker, trim to `top_n`. Apply to both branches (continuation picker; reversal already over-fetches via `REVERSAL_POOL_N` — confirm and add ticker-dedup there too if absent). Check `cockpit/routers/picks.py` for a mirrored query and apply the same rule.

**Commit** — `fix(notify): dedup daily picks by ticker before the top-N trim`.

### Task E5: Model + reasoning knobs (bicep is the source of prod truth)

**Findings/levers:** reasoning `high → medium`; Coach/Auditor prose to Haiku.

**Files:**
- Modify: `infra/modules/jobs.bicep:118` — `param analysisReasoning string = 'medium'` (comment: thinking bills as output at $25/MTok; 2026-07-17 cost plan)
- Modify: `src/swing_screener/journal/coach_run.py:44` and `src/swing_screener/journal/audit_run.py:37` — `_MODEL = "claude-haiku-4-5"`; also flip the default `model` params in `coach_author.py:76` / `audit_author.py:57` so the stamped provenance matches (the audit's *duplicated model-id constant* finding — have the runners pass `_MODEL` explicitly to the authors so there is exactly one constant per worker)
- Test: update the assertions that pin the model strings; add none.

**Note:** `_MODEL_PRICES` already covers haiku after E3.1, so the coach cap keeps metering.

**Commit** — `feat(journal): coach/auditor prose on haiku-4-5; digest reasoning default medium`.

### Task E6: Market Weather gets an off-switch and a cap

**Finding:** *analyze_market_deep — hard-coded ON, no env switch, no dollar cap*.

**Files:** `src/swing_screener/config.py` (StrategyConfig market knobs → read env overrides `SWING_MARKET_REPORT`, `SWING_MARKET_MAX_USD` — follow the exact `settings.py` parse conventions: absent = current defaults, honest fail-safe), `src/swing_screener/notify/market_run.py` (skip the LLM when disabled or when the single call's captured cost would matter — enforce as a 0-disables switch plus usage persistence on the MarketReport row), `infra/modules/jobs.bicep` (add both envs to commonEnv with defaults matching current prod behavior: on, `'1.00'`); tests.

**Commit** — `feat(market): env switch + spend visibility for the weekly Market Weather LLM`.

---

## Phase F — Digest cadence correctness — branch `fix/digest-cooldown-cadence`

### Task F1: Per-kind cooldown

**Finding:** *Daily-run-denominated cooldown applied unchanged to weekly/monthly digests, dropping most persistent 1wk/1mo setups* — `notify/run.py:429`.

**Fix:** per-kind `max_age_days`: daily = `digest_repeat_cooldown_days` (1), weekly = 5 runs, monthly = 21 runs (constants beside `_PICKERS`, documented). Log `"cooldown dropped %d picks for %s digest"` whenever it filters, so a blanked digest is diagnosable. Failing test: a 1wk signal first seen 4 daily runs ago must appear in the weekly digest but not the daily.

---

## Phase G — Infra/CD hardening — branch `fix/infra-cd-hardening`

Each task is small and independent; commit per task.

**G1 — `notify.run` gets the sqlite-in-cloud guard + startup migration** (medium): mirror the other four entrypoints in `notify/run.py:805` `main()` — `--db` defaults None, `db_url = _resolve_db_url(args.db)`, `if db_url.startswith("mssql"): _migrate_with_retry(db_url)`. Failing test: `KEY_VAULT_URL` set + sqlite URL → refuses.

**G2 — `market_run` gates its migrate on mssql** (medium): `notify/market_run.py:124` — wrap in `if db_url.startswith("mssql"):`. Test with sqlite URL asserting `migrate_fn` NOT called.

**G3 — `imageTag` default** (medium): `infra/modules/jobs.bicep:45`, `infra/main.bicep:31`, `infra/main.bicepparam:22` — default `'latest'` (cd.yml already pushes `:latest` alongside the sha). Comment referencing the deepAnalysisEnabled lesson. No test; verify with `az bicep build`.

**G4 — CD gains the single-alembic-head guard** (medium): copy ci.yml:28-31's step into cd.yml's `test` job verbatim.

**G5 — CI double-run** (low): add `branches: [main]` filter (or `paths-ignore` symmetry) to ci.yml's `push` trigger so PR commits run once and merges twice at most.

**G6 — SQL token leak** (low, security): in the workflow step that exports the live SQL access token, stop writing it to `GITHUB_ENV`; pass it as a step-scoped `env:` on the consuming step only.

**G7 — Cockpit DB grant** (medium): the grant script/docs say read-only but the cockpit writes (DISARM events, journal, acks). Update `infra/` grant SQL (db_datawriter or targeted GRANTs on the written tables) + the runbook. Verify by exercising DISARM against a grant-parity local mssql if available; otherwise document the manual prod step in the PR.

**G8 — stale bicep secret docs** (low): fix the `secretNames` description (two secrets, not four; Gmail is gone) in `infra/modules/jobs.bicep` + `main.bicep`.

---

## Phase H — Data-cache correctness — branch `fix/fetch-cache-poisoning`

**H1 — Pre-close fetch must not poison the day cache** (high→medium, verified): `data/fetch.py:94-99` — have `_drop_in_progress_daily_bar` return `(df, dropped: bool)`; when `dropped`, skip `df.to_parquet(cache_file)` (return uncached). Failing test: intraday fetch (mock now=14:00) then post-close fetch same day → second call re-downloads and caches the full frame. Also fix the half-day hole: the guard's `now.hour < 16` check drops a COMPLETE bar after a 1pm close — acceptable to leave with a TODO comment referencing the finding (needs a market-calendar dependency to do properly); the no-cache change already un-pins it.

**H2 — GEX intraday settle guard** (medium): `options/run.py:56` / `settle.py` — only apply the `eod_flat` fallback when the frame's last 5m bar is ≥ 15:55 ET for the session day; otherwise leave open + count skipped. Refuse `settle` before 16:00 ET without `--force`. Failing tests for both behaviors.

**H3 — GEX `--cache-dir` silently ignored** (medium): wire the parsed arg through to the workers (they currently resolve `load_settings().cache_dir`); or delete the flag. Wire it — test asserts a custom dir is used.

---

## Phase I — DB indexes — branch `perf/hot-path-indexes`

One migration + model edits (`index=True`) for:
- `signals.run_date` (`ix_signals_run_date`) — every cockpit picks poll + nightly screen
- `paper_trades.status` — composite `(status, account)` matching the execution-gate queries (`ix_paper_trades_status_account`)
- `exit_events.created_date` (`ix_exit_events_created_date`)

Also fix *Unique index on nullable `option_paper_trades.import_key`* in the same migration: drop + recreate as a filtered unique index (`mssql_where=import_key IS NOT NULL`; sqlite ignores it harmlessly via a conditional op or `render_as_batch`). Local dbs: note in the migration docstring that create_all-born local.db needs `alembic stamp` first (existing convention). Verify: `alembic upgrade head` on a fresh sqlite + `alembic heads` single.

---

## Phase J — Cockpit API robustness — branch `fix/cockpit-api-degrade`

Small fixes, one commit each, per-row-degrade posture throughout:
- **J1** corrupt `facts_json`/`findings_json` rows: wrap per-row parse in try/except, skip + log, never 500 the list (coach-reviews + audit lists).
- **J2** `/api/coach/weaknesses`: handle the model's own `"[]"` default for `items_json` (accept list-or-dict shape, normalize).
- **J3** heartbeats sidecar read: guard `newest_verdicts_mtime` stat calls like events.py does.
- **J4** GEX build/settle POSTs: catch upstream network failures → 503 with class-name-only detail (match cockpit posture).
- **J5** manual close atomicity: make the close an `UPDATE … WHERE status='open'` and 409 on rowcount 0 (removes the check-then-act window).
- **J6** `loss_r` gauge under mode `off`: scope the sum away from the research shadow grid exactly like the concurrent gauge's fix in the same block.
- **J7** `spend_rows_since`: push the date cutoff into the SQL WHERE clause.

---

## Phase K — Cockpit UI fixes — branch `fix/cockpit-ui-polish`

- **K1** error swallowing: coach tag-confirm + audit ACKNOWLEDGE (two panels) surface failures via the existing error-pill pattern instead of silent catch.
- **K2** CSV import: reset the file input's value after handling so the same file can be retried.
- **K3** `fmtResult` → route through `lib/fmt` (fixes `$-42` and float precision); `null realized_r` no longer styled as a win; `null max_drawdown` renders `—` not `0.00R`.
- **K4** keyboard: give the open-book scroll region `tabIndex` + the explicit `onKeyDown` arrow-scroll handler (existing pattern from the focused-overflow convention).
- **K5** drop dead CSS-class references (`ltf-btn-quiet`, `jr-weak`, `jr-weak-list`) or add the missing rules; delete the dead `requested_at` null-check; consolidate the four private time/date helper copies into `lib/fmt.ts`.
- **After every CSS edit:** run the brace-balance count (repo convention).

---

## Phase L — Dead-code sweep — branch `chore/dead-code-sweep`

Deletions (each re-verified zero-reference by the audit's verifiers; re-run `git grep -n <name>` before each delete as insurance):
- `cancel_all_orders` (Protocol + FakeBroker + Alpaca impls) — the blanket-cancel footgun
- `db.repo.latest_signals`, `db.repo.update_trade`
- `fetch_universe`, `fetch_vix` (production bypasses both)
- `analytics.performance.rank_bucket` / `score_bucket` public wrappers
- `analytics.pl.total_unrealized_pl`
- `pipeline.analyze.analyze_ticker` wrapper (test-only; rewrite its 5 test call sites over `analyze_frames(build_frames(...))`)
- `journal/discipline.py` dead ImportError fallback
- `add_thesis` + `JournalThesis` repo writer (decision #4: keep table)
- `conviction_sizing` / `conviction_weight_*` config knobs (weights live in the hardcoded dict; delete the dead knobs, point a comment at the real source)
- stale `storage/__init__.py` facade: re-export all six or (simpler) delete the facade re-exports and have callers import `storage.blob` directly — pick whichever `git grep` says is smaller; audit says no consumer uses the facade, so delete.
- **Annotate only (decision #3):** `option_review_facts` — add a `# NOTE: not wired; robinhood coach reviews pending product decision` comment.

Delete each item's orphaned tests in the same commit. Full suite + `ruff` after each deletion.

---

## Phase M — Dev ergonomics (lowest priority) — branch `chore/replay-and-test-structure`

- **M1** `scripts/_replay_common.py`: shared `load_replay_corpus(exclude=("^VIX","SPY"))` helper; migrate all 18 replay scripts (the 9-vs-9 exclusion drift is already a correctness smell — pick the exclusion set deliberately and document it).
- **M2** split `tests/cockpit/test_api.py` (4,506 lines) along the existing per-router test-file convention; no test-body changes, moves only.
- **M3** `pipeline/reflect.py` low-severity drift pair (empty-corpus warning promise; re-arm counter counts full forward book vs gold facet grader) — align the messages/counters with actual behavior.
- **M4** notify low items: blob-chart `except: pass` → log.warning; on-demand email dedup key includes request date not today; `ProposedOrder.instruction()` respects the stored `side`; ACS senderAddress empty-string guard; `autonomy_gate` computed once per run and threaded through.

---

## Suggested PR order & verification gates

| Order | Phase | Gate before merge |
|---|---|---|
| 1 | A (live-money) | full pytest + the two new regression tests; manual read of the delete predicate |
| 2 | B (queue claim) | pytest + **post-deploy: queue one on-demand request and watch it complete** |
| 3 | C (NaN) | pytest incl. new NaN cases |
| 4 | E (cost) | pytest; **post-deploy: watch `analyst_calls.input_tokens`/`est_cost_usd` for a week — expect input well under the 400k/run baseline** |
| 5 | D (auditor) | pytest; next Saturday audit shows clamps ≠ breaches |
| 6 | F, G, H, I | pytest + `az bicep build` for G; `alembic heads` == 1 for I |
| 7 | J, K | pytest; cockpit smoke via vite dev + Playwright MCP; CSS brace count |
| 8 | L, M | full suite + ruff after every deletion |

Rough sizing: A–E are a few sessions of focused work; F–I are each under an hour; J–M are batchable whenever.

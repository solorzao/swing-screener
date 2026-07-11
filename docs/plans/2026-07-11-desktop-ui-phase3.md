# Desktop Cockpit Phase 3 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task. (Revision 2 — 37 findings from a four-lens adversarial verification
> pass folded in; the notable ones are marked ★ inline.)

**Goal:** The remaining screens (Candidates, Positions & Ledger, Playbooks, Analyst,
Execution Safety, Market Weather, Reference, Systems), Mission Control Zones B/D/E, and
the SIX ACTIONS (log trade, close trade, request deep analysis, approve proposal,
withdraw proposal, DISARM) — after which the Streamlit dashboard is deleted entirely.

**Architecture:** Backend grows action POSTs (all cloning the `/api/azure-login`
template: X-Cockpit guard → 403, precondition → 409, plain-dict response) and per-screen
read endpoints on `cockpit/api.py`, fed by two new injectable seams on `create_app`
(`latest_closes_fn` quotes and `broker_factory`, both TTL-cached with single-flight
locks). Three modules relocate out of `dashboard/` before it dies (`quotes` → `data/`,
`pl` → `analytics/`, blob resolvers → `storage/blob.py`). Verdict cost/corpus stamping
and the proposal-decision machinery land in `pipeline/`. Frontend gains a screens
scaffold (App-level state + `1–9` keys — no router: `StaticFiles(html=True)` is not an
SPA fallback and pywebview loads the root URL once), per-screen mounting with an
explicit permanent-poll roster, and the Phase-3 component set. Design:
`docs/plans/2026-07-05-desktop-ui-design.md`.

**Tech Stack:** Existing only — FastAPI/SQLAlchemy/Alembic; React 19 + TS (no router,
no hotkey lib, no chart lib); one Alembic migration (`trades.override`).

**Conventions that bind every task:** TDD; `@dataclass(frozen=True, kw_only=True)`;
narrative invariant docstrings; hand-rolled wire dicts; leak posture (class names only);
routes before the static mount; Literal query params; X-Cockpit on every POST; every
statistic a 10-key Stat (levels, prices, counts, caps are engine facts and ride plain —
the funnel/win-rate precedent); ruff + mypy clean; tests annotated; EVERY frontend task
runs `npm run lint && npm run build` and commits the regenerated `static/` in the same
commit — no exceptions (the CI drift check fails otherwise); commit trailer
`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

## Scope decisions (read before executing — the judgment calls)

1. **DISARM is the venue sweep, and only that.** `POST /api/disarm` wraps
   `pipeline/disarm.pull_entry_orders` + `ensure_stop_protection` (PR #98 semantics:
   cancels entry-side, keeps/restores bracket stops, never closes positions, never
   computes a stop level — restore levels are COPIED from ExecutionLog tickets). It does
   NOT flip `SWING_EXECUTION_MODE` — env is per-process and the Azure jobs own theirs;
   the runbook's `az containerapp job update` remains the remote mode flip. The button
   caption says exactly this. Hold-to-confirm (900ms) fires the `dry_run=1` preview on
   hold-start and ★ the real POST only arms once the preview has landed — a completed
   hold before the preview renders waits, it does not fire blind.
2. **Approve/withdraw proposals never auto-promote.** `propose.py` pins promotion as a
   HUMAN act and the roster is Python source guarded by the registry lockstep test.
   APPROVE = status flip to `approved` + rationale append + a Needs-Your-Hand item
   carrying the three-artifact promotion checklist (variants.py roster line,
   experiments.json registry row, proposed.json flip — one commit). WITHDRAW = the
   established convention (`withdrawn` + `WITHDRAWN <date>: <reason>`). Two hazards get
   fixed first: `draft_variants` must MERGE (preserve non-queued rows), and ★ merged
   files need a name-collision rule — draft names are deterministic
   (`<pt>_<hunch-slug>_<index>_q`), so a decided row's name WILL be re-drafted for a
   durable hunch: a name existing with status != queued blocks a fresh queued draft of
   that name (which also usefully prevents re-queueing what a human withdrew). The UI
   states the write is a local working-tree edit in `edge/` committed with the decision.
3. **Override flagging is one nullable column.** `trades.override` (String(256), None):
   a server-computed deviation summary stamped at insert when the log was prefilled from
   a pick, and `signal_id` is always set from the prefill source (exitcheck's play-type
   resolution needs it). ★ None conflates "engine-faithful" with "manual, never
   prefilled" — the UI disambiguates via `signal_id`: null-signal rows render an
   "unlinked (manual)" tag; override text renders only when non-null. One Alembic
   migration — and ★ the local-sqlite recovery is NOT a bare `alembic upgrade head`
   (empirically it fails on a create_all-born local.db: no alembic_version stamp →
   replays the initial migration → "table email_log already exists"). The working
   recovery, documented in Task 22 and retro-fixed into docs/cockpit.md's existing
   troubleshooting section: `alembic stamp <pre-phase3 head>` then `alembic upgrade
   head` (alembic ships in the `[azure]` extra — `pip install -e ".[azure]"` first), or
   the one-liner `ALTER TABLE trades ADD COLUMN override VARCHAR(256)`.
4. **Manual closes write an ExitEvent** (`reason='manual_close'`, `is_paper=False`,
   `tier=""`) — it feeds the Zone E ticker, moves the SSE `exit` watermark, and gives
   real trades the same audit trail. ★ `pending_exit_alerts` selects ALL real-book
   ExitEvents and the hourly exit job + digest would EMAIL an urgent alert for a close
   Oliver just performed himself — `manual_close` is excluded from the alert query, with
   a test.
5. **Quotes are TTL-cached server-side and honestly labeled.** A 10-minute in-process
   cache (misses cached too — upstream failures are not negative-cached and a dead
   ticker costs ~1.5–2.5s of retry sleeps plus network timeouts per call) wraps the
   relocated `data/quotes.latest_closes`, behind a single-flight lock (★ the engine
   cache documents this exact threadpool race — copy its posture). Prices are last
   COMPLETED daily closes — every price surface says "as of last close". Quotes never
   run inside the SSE token loop.
6. **BracketShield requires the broker; UNKNOWN is a designed state.** A TTL-cached
   (60s, single-flight) snapshot of positions+open orders serves the lamps;
   `build_broker` returning None (SWING_BROKER unset — today's reality) renders
   UNKNOWN, never green. ★ `broker_configured` (settings truthiness, NOT connectivity)
   is a first-class wire field — broker-unset and broker-unreachable are different
   states with different UI (DISARM disabled vs enabled-but-degraded).
7. **Zone D shows-and-flags; it never widens the pick set — and parity means the
   digest's ORDER.** ★ The digest drops non-actionable picks BEFORE the sector cap so
   stale picks free their top-5 slots for backfill (the exact Jul-2 rotation lesson).
   `/api/picks` computes the digest's surfaced set with the digest's order (pool →
   play-type-aware liveness drop at the cached quote → sector cap → 5) and ADDITIONALLY
   returns the liveness-dropped picks flagged as extras — they never consume cap slots,
   so the five always match the email. Selection knobs come from StrategyConfig
   (`daily_max_per_sector`, BOTH `reversal_surface_confirmed_only` AND
   `reversal_surface_premium_only`) — parity by construction, not by today's defaults.
8. **Verdict gains `cost_level`/`corpus_id` (None defaults) and reflection gets
   `--as-of` pinning + a `--verdicts-only` regen mode.** Replay rows stamp `'0.05'` +
   the corpus stamp; forward rows `cost_level_for(book)` + corpus None. ★ Stamping
   happens in `run_reflection` post-`grade` via `dataclasses.replace` (grade stays
   pure); `--verdicts-only` IMPLIES force-all-play-types (the due-gate would otherwise
   no-op the regen) and skips md authoring AND the drafter (a plain `--force` either
   burns an unnecessary Opus re-author or, keyless, clobbers the prose with the
   deterministic template — and re-runs the drafter; wrong for a stamping regen).
9. **Keyboard ships `1–9` only.** One App-level keydown with the typing-context guard
   (INPUT/TEXTAREA/SELECT/contentEditable + modifier bail) landing WITH the scaffold,
   before any form exists. The `/` palette, `j/k` rows, ★ and Enter drill-in (which
   depends on row focus existing) are deferred together. Digits follow the design's
   fixed numbering (1 Mission Control … 9 Systems; 10 Reference gets a masthead link).
10. **Tray + notifications and PyInstaller are DEFERRED, with evidence** (pystray absent
    from the repo; every useful tray behavior is a foreign-thread WinForms call — the
    documented 2026-07-06 deadlock pattern; abort criteria written in non-goals).
11. **The first green is TierChip on `forward_confirmed` verdicts — and ★ zero such
    verdicts exist today** (continuation all-hunch; reversal carries 3 replay_screened,
    which render amber). Playbooks ships amber-and-gray and that is correct. Tier rides
    verdict wire rows, NOT the Stat dict (closed key set).
12. **Post-action wake:** every successful action POST bumps a server-side nonce riding
    the SSE token (single-process uvicorn daemon thread — a module counter under a lock
    suffices); the acting component also refetches locally via a key bump.
13. **One branch, one squash PR** (`feat/cockpit-phase3`); sanctioned split after
    Task 12 (backend PR + frontend/retirement PR) if review size demands.
14. **Analysis "unread" is client-side** (localStorage last-seen id) — no migration.
15. ★ **The permanent-poll roster is explicit** (per-screen mounting kills everything
    else): App permanently polls health, heartbeats, gate (now carrying
    `broker_configured` for the masthead DISARM enablement), forward-books (the
    Needs-Your-Hand strip lives above the screen switch), and the new lightweight
    `/api/attention` (proposal/reflection-due/analysis counts for the strip). Everything
    else mounts with its screen. Forward-books + gate + attention ≈ 1.3s/60s server
    math; the heavy per-screen endpoints only poll while visible.
16. ★ **Zone E substitutes AnalysisRequest for the design's "reflections" source** — no
    reflections table exists (reflection's artifacts are edge/ files and PRs); the
    verdicts-file mtime already rides the SSE token, and a synthetic reflection event
    can anchor on it later if wanted.

---

### Task 1: Relocations — `quotes` → `data/`, `pl` → `analytics/`, blob resolvers → `storage/`

The deletion blockers. `notify/run.py:816` lazily imports `dashboard.quotes` inside
`main()` — pytest stays green if this breaks, but the PRODUCTION daily digest dies.

**Files:** Create `src/swing_screener/data/quotes.py`, `src/swing_screener/analytics/pl.py`
(verbatim moves); Modify `src/swing_screener/storage/blob.py` (add
`resolve_chart_bytes(chart_path) -> bytes | None` / `resolve_pdf_bytes(key) -> bytes |
None` — unified on bytes: the dashboard's local branch returned a PATH STRING because
st.image accepts paths; FastAPI needs bytes → `Path.read_bytes()` if exists else None;
blob branch → `download_bytes` with any-exception → None), `notify/run.py` (the import),
`dashboard/app.py` (import from new homes), `dashboard/quotes.py`+`pl.py` (one-line
re-export shims, deleted in Task 21); Tests: move `tests/dashboard/test_quotes.py` →
`tests/data/`, `test_pl.py` → `tests/analytics/`; re-point the three
`test_dashboard_resolve_*` in `tests/test_storage_blob.py` (★ monkeypatch
`swing_screener.storage.blob.download_bytes` — the resolvers' OWN module globals; the
old pattern patched the dashboard module's from-import); ADD the missing
pdf-resolver test.

**Steps:** failing tests → move → shim → re-point → grep-verify zero non-shim
`dashboard.quotes`/`dashboard.pl` imports → full `pytest -q`, ruff, mypy. Commit:
`refactor(dashboard): quotes/pl/blob-resolvers move out -- deletion prerequisites`

---

### Task 2: Verdict cost/corpus stamping + pinned, sidecar-only reflection regen

**Files:** Modify `pipeline/reflect.py`, `pipeline/replay.py`; Test
`tests/pipeline/test_reflect*.py` (extend).

**Steps:**
1. Failing tests: `Verdict` += `cost_level: str | None = None`, `corpus_id: str | None
   = None` (defaults MANDATORY — `load_verdicts` is `Verdict(**d)` and committed
   sidecars lack the keys); old-sidecar round-trip; stamping (replay rows `'0.05'` +
   corpus stamp, forward rows `cost_level_for(forward_gold)` + None, `source='none'`
   rows follow the displayed book).
2. ★ Stamping seam: `run_reflection` stamps post-`grade` via `dataclasses.replace`
   (grade stays pure); it gains a `corpus_id: str | None = None` param threaded from
   `main`.
3. Promote `replay._cached_daily_file`/`_load_cached_daily` public; `reflect.main`
   gains `--as-of` (pinned loading + `corpus_stamp(cache_dir, tickers, as_of)`;
   unpinned default stays — the stamp then records the actual mixed vintages).
4. ★ `--verdicts-only`: grade + write sidecars ONLY — implies force-all-play-types (the
   due-gate would no-op a regen), no md authoring, no drafter, no frontmatter reset.
5. Gates. Commit: `feat(reflect): verdicts carry cost level + corpus id -- pinned,
   sidecar-only regen`

---

### Task 3: Proposal decisions — merge + collision rule + the transition helper

**Files:** Modify `pipeline/reflect.py` (`draft_variants`), `pipeline/proposed.py`;
Test `tests/pipeline/` (extend).

**Steps:**
1. Failing tests: `draft_variants` preserves non-queued rows (seed withdrawn+approved,
   draft, both survive beside the fresh queued set); ★ COLLISION: a preserved decided
   row whose deterministic name (`_draft_name` is `<pt>_<slug>_<index>_q` — durable
   hunches re-draft at the same index) matches a fresh draft BLOCKS that fresh draft
   (decided rows win; a withdrawn idea is not silently re-queued).
2. `decide_proposal(edge_dir, play_type, name, *, decision: Literal['approved',
   'withdrawn'], reason, today) -> ProposedVariant`: KeyError unknown name; ValueError
   unless status is `queued` (approve) or `queued`/`approved` (withdraw); status +
   `f' {decision.upper()} {today}: {reason}'` rationale append; rewrite via
   `dataclasses.replace`.
3. Gates. Commit: `feat(proposals): decisions survive redrafts -- merge, collisions,
   the first tool-written transition`

---

### Task 4: Cockpit seams — TTL quote cache + broker snapshot (single-flight)

**Files:** Create `src/swing_screener/cockpit/livedata.py`; Modify `cockpit/api.py`
(`create_app` gains `latest_closes_fn`, `broker_factory` seams); Test
`tests/cockpit/test_livedata.py`.

**Steps:** `QuoteCache(fetch_fn, ttl_s=600, clock)` — one upstream call per TTL window,
misses cached, `as_of` exposed, ★ `threading.Lock` single-flight (the engine cache at
api.py:219 documents this exact threadpool race — same posture; test with two threads
racing an expired TTL: one upstream call). `BrokerSnapshot(broker_factory, ttl_s=60,
clock)` — `(positions, open_orders, as_of) | None`; factory None → None; broker
exception → None cached until TTL; same lock. `create_app` defaults bind
`data.quotes.latest_closes` over `load_settings().cache_dir` and
`lambda: build_broker(load_settings())`. Commit:
`feat(cockpit): quote + broker seams -- TTL, single-flight, degrade-never-crash`

---

### Task 5: Trade WRITES — migration, log trade, close trade

**Files:** Modify `db/models.py` (Trade += `override: Mapped[str | None]` String(256)),
`db/repo.py` (close_trade already-closed guard), `notify/select.py` (★
`pending_exit_alerts` excludes `reason='manual_close'` — without this the hourly exit
job emails an urgent alert for a close Oliver just performed), `cockpit/api.py`;
Create `alembic/versions/<newrev>_trade_override.py` (down_revision = current head —
verify `alembic heads`; nullable, no server_default); Test `tests/cockpit/test_api.py`,
`tests/notify/` (alert exclusion), `tests/test_alembic_offline.py` (extend).

**Endpoints:**
- `POST /api/trades` — X-Cockpit; Pydantic validation (repo validates NOTHING): ticker
  required→upper, entry>0, size>0, stop<entry, target>entry; server stamps
  entry_date=today; sets `signal_id` when prefill-sourced; computes + stores `override`
  (entry outside [floor, ceiling] in zone-R; stop/target moved in %; None when faithful
  or unprefilled — the UI's unlinked-tag handles the distinction per scope 3).
- `POST /api/trades/{id}/close` — X-Cockpit; exit_price>0; reason ≤32 chars default
  `manual`; 404 unknown, 409 already closed; writes
  `ExitEvent(reason='manual_close', is_paper=False, ★ tier="", account='research')`;
  response carries computed realized R and $.

**Named tests:** validation matrix (each rule rejects), 404/409, override stamped /
None-when-faithful / None-when-unprefilled, ExitEvent written with tier="",
`test_manual_close_never_emails` (pending_exit_alerts excludes it), migration in
EXPECTED_TABLES-adjacent assertions + the (max) sweep covers the new column. Commit:
`feat(cockpit): log + close trade -- engine defaults, override stamping, quiet closes`

---

### Task 6: Trade READS — positions + trade-defaults

**Files:** Modify `cockpit/api.py`; Test `tests/cockpit/test_api.py` (extend).

**Endpoints:**
- `GET /api/positions` — open real Trades + open `account='live'` PaperTrades; per-row
  `position_pl` guarded per-row (one malformed trade never 503s the zone; missing quote
  → null P/L fields, row kept); ★ live PaperTrade rows have NO size column: render
  R-multiple from `risk` with dollar P/L null unless an ExecutionLog `shares` join
  (ticker + status filled_live/submitted_live, newest) succeeds — spec'd, not
  improvised; badge state (red price≤stop / yellow ≥target); per-position bracket lamp
  from the snapshot (UNKNOWN without a broker); ★ `last_close: float | null` per row
  (the close form's prefill source); the three cap gauges (`execution_logs_for_day`
  notional $, `realized_r_on` in R — the loss cap is an R THRESHOLD, label it,
  `count_open_positions`; account by `execution_mode`; run_date = `latest_run_date`;
  None cap = unbounded, never 0/0); closed trades + realized equity points;
  `quotes_as_of`. unrealized_pct is a FRACTION on the wire — documented.
- `GET /api/trade-defaults?signal_id=` — Signal levels, actionability at the cached
  quote, `size_order` via `resolve_risk_unit` (shares==0 = "sizing unconfigured"),
  suggested entry = cached close clamped to the zone.

**Named tests:** per-row degradation (malformed + missing-quote), live-row R-only vs
shares-join, caps math mirrors `_limit_block` semantics, unbounded-cap shape, defaults
prefill incl. sizing-unconfigured. Commit:
`feat(cockpit): positions + trade defaults -- per-row degradation, honest caps`

---

### Task 7: Deep analysis — request, list, asset proxies

**Files:** Modify `cockpit/api.py`; Test `tests/cockpit/test_api.py` (extend).

`POST /api/analysis` (X-Cockpit; ticker→upper; server stamps
`requested_at=datetime.now(UTC)` — the worker compares UTC; note the queue-view reorder
in the docstring); `GET /api/analysis?limit=` (+ derived `stalled` when running >30min,
mirroring the worker's requeue window; `worker: 'cloud (*/15min)' | 'manual'` derived
from Azure-vs-local DB — the UI copy for local says requests wait for
`python -m swing_screener.notify.ondemand`); `GET /api/analysis/{id}/chart/{index}`,
`GET /api/analysis/{id}/pdf`, `GET /api/signals/{id}/chart` — server-side resolution
via the Task-1 resolvers ONLY (never client paths/keys — arbitrary-read hole), bytes
with proper media types + Content-Disposition `{ticker}_report.pdf`, 404 on
unresolvable (aged-out blob / missing local file / bad index — most signals have no
chart; 404 is normal). ★ Blob tests monkeypatch `storage.blob.download_bytes` (the
resolvers' own globals) + setenv `SWING_BLOB_ACCOUNT_URL`; never import azure or
charts.render. Named tests: uppercasing, UTC stamp, stalled flag, both proxy branches,
404 paths, traversal rejection. Commit:
`feat(cockpit): deep-analysis action + report asset proxies`

---

### Task 8: Proposals API — list, approve, withdraw

**Files:** Modify `cockpit/api.py`; Test `tests/cockpit/test_api.py` (extend).

`GET /api/proposals` — both play types via `load_proposed_for`; rows: the 8 fields +
`gate_verdict` (`to_config(pv, StrategyConfig())` → 'ok' | the ValueError text) +
`delta_vs_incumbent` (knob: current → proposed) + `noop: bool`.
`POST /api/proposals/{play_type}/{name}/approve|withdraw` — X-Cockpit; body
`{reason}` required ≤200; 404/409 mapping `decide_proposal`'s errors; approve response
carries the verbatim three-artifact promotion checklist; both responses name the file
touched + "uncommitted working-tree edit — commit with your decision". Named tests:
both transitions, wrong-state 409s, checklist text, the file actually rewritten.
Commit: `feat(cockpit): proposal decisions -- approve marks, never promotes`

---

### Task 9: DISARM + Execution Safety endpoint

**Files:** Modify `cockpit/api.py`, ★ `pipeline/disarm.py` (ensure_stop_protection
returns `(restored: list[str], unprotected: list[str])` — restored symbols are
currently only log lines, dry-run included so the hold preview can show them; update
its callers: `disarm.main`, the kill-switch block in `notify/run.py` ~661-681, and
existing disarm tests), `pipeline/preflight.py` (★ accepts `broker: BrokerClient |
None` — today `_check_is_real_money` calls `broker.is_real_money()` un-guarded and a
None broker AttributeErrors out of preflight(): with None, config reads NO-GO
"no broker configured", reachable/funded/is_real_money read as explicit not-applicable
lines, matching the CLI's early-exit wording); Test: cockpit + pipeline test extensions
(FakeBroker-driven; fill() first so bracket legs exist).

**Endpoints:**
- `POST /api/disarm?dry_run=0|1` — X-Cockpit; 409 `no broker configured` on None
  factory; response `{dry_run, cancelled: [{symbol, broker_order_id}], sells_kept: int,
  stops_restored: [symbol], unprotected: [symbol]}` (now derivable from the changed
  return); stop levels COPIED never computed; snapshot cache invalidated after a real
  run.
- `GET /api/execution/safety` — ★ `broker_configured: bool` (settings truthiness — NOT
  connectivity; DISARM enablement keys on it, and it ALSO rides `/api/gate` so the
  always-visible masthead needs no extra poll); preflight report (None-broker branch
  per above — never a 500 on the default local setup); the three locks + caps mandate;
  mode + `env_scope: 'this process'`; bracket-shield table (UNKNOWN-never-green).
  Broker exceptions degrade to NO-GO/UNKNOWN lines.

**Named tests:** disarm dry-run cancels nothing (FakeBroker order intact), real run
cancels buys only + keeps sells, restored list populated, unprotected loud, 409
no-broker, safety with None factory returns 200 + broker_configured false + no 500,
gate carries broker_configured. Commit:
`feat(cockpit): DISARM + execution safety -- venue truth, copied stops, no-broker is a
state not a crash`

---

### Task 10: Picks, ticker, exits, reference reads

**Files:** Modify `cockpit/api.py`; Test `tests/cockpit/test_api.py` (extend).

**Endpoints:**
- `GET /api/picks` — ★ digest ORDER: `daily_picks(top_n=5, max_age_days=cooldown,
  max_per_sector=StrategyConfig().daily_max_per_sector)` and
  `reversal_picks(pool REVERSAL_POOL_N, confirmed_only=scfg.…, premium_only=scfg.…)` →
  play-type-aware liveness drop at the cached quote (reversals keep `extended`) →
  `cap_signals_by_sector(limit=5)`; the surfaced five ALWAYS match the email (drops
  free slots for backfill); liveness-dropped picks ride as flagged EXTRAS that never
  consume cap slots. Per pick: LevelRail fields, conviction_tier, actionability
  `{status, dist_r}`, `is_repeat`, `has_chart`, cohort ref `(play_type, strength)`,
  today's AnalystCall grade + that grade's scored `(n, mean_r)`; `quotes_as_of`.
- `GET /api/ticker?limit=` — merged reverse-chron: ExitEvent + ExecutionLog + EmailLog
  + AnalystCall + AnalysisRequest (★ the design's "reflections" source is substituted —
  no reflections store exists; noted in the docstring), per-source `ORDER BY id DESC
  LIMIT n` merged in Python, DATE-only sources `_eod_utc`-anchored, (source, id)
  tiebreak; row `{source, ts, ticker|null, headline, detail}` ★ + `account`,
  `is_paper`, `reason`, `tier` on exit-source rows.
- ★ `GET /api/exits?reason=&book=&account=&limit=` — the Exit Log page's three facets
  as a filterable read (Book=is_paper and Account are DIFFERENT axes: the research grid
  and the curated intent book are both is_paper=True) — Task 21's deletion gate needs
  this surface.
- `GET /api/universe?search=` (ticker-LIKE parity + the never-displayed `sector`
  column), `GET /api/emails?limit=` (default 100 — list_email_log is unbounded).

**Named tests:** `test_picks_match_digest_surfaced_set` (seed a droppable stale
reversal: the surfaced five match the digest's backfilled set AND the dropped pick
rides flagged), `test_picks_reversal_extended_is_normal`,
`test_ticker_merges_sources_desc`, `test_exits_three_facets`,
`test_emails_limit_default_100`. Commit:
`feat(cockpit): picks (digest parity + flagged extras), ticker, exits, reference`

---

### Task 11: Playbooks, weather, analyst, attention endpoints

**Files:** Create `src/swing_screener/cockpit/playbooks.py`; Modify `cockpit/api.py`,
`db/repo.py` (`analyst_call_freshness` + export the 10-day window constant); Test
`tests/cockpit/test_playbooks.py`, `tests/cockpit/test_api.py` (extend).

**Steps:**
1. `playbook_drift(md_text, verdicts) -> DriftReport` — PURE, STRUCTURAL: each
   verdict's `dimension=bucket` token must appear in the md section matching its tier;
   NEVER numeric comparison (the author rounds); advisory amber (hand-edits are
   legitimate).
2. `GET /api/playbooks` — per play type: md verbatim (numbers on screen come from the
   SIDECAR only), frontmatter, verdict rows (all fields incl. cost/corpus; Verdict has
   NO ci_high and ci_low is Bonferroni-corrected — the wire says so; n=0 renders
   "empty on this corpus"), drift report, reflection-due (`due_play_types`), falsified
   section body.
3. `GET /api/weather` — latest MarketReport (all columns + core + report + `is_deep` —
   badge the deterministic fallback) + history `[{run_date, ha_alignment, flipped,
   core}]`; `{weather: null}` on empty.
4. `GET /api/analyst` — calibration reshaped to objects WITH zero-count grades; nudge
   attribution; `analyst_call_freshness(calls, today)` → `{scored, pending_in_window,
   expired_unfilled}` (the "unfilled fraction"; new pure helper beside
   `score_analyst_calls` so endpoint and reflection agree by construction);
   calibration progress with `ci_low=-inf` → null; spend today/7d/30d (+ NULL-cost
   undercount note); all R labeled shadow-book.
5. ★ `GET /api/attention` — the permanent-poll strip feed: `{proposals_queued: [name],
   proposals_approved_pending: [name], reflection_due: [play_type],
   latest_analysis_id: int | null}` — cheap file+DB reads, no broker, no quotes.

**Named tests:** `test_drift_flags_missing_token_in_wrong_tier_section`,
`test_drift_ok_on_faithful_md`, `test_playbooks_verdicts_carry_cost_corpus`,
`test_weather_null_then_latest`, `test_analyst_inf_ci_serializes_null`,
`test_calibration_includes_zero_count_grades`, `test_unfilled_fraction_three_way`,
`test_attention_shape`. Commit:
`feat(cockpit): playbooks with drift lamp, weather, analyst, attention feed`

---

### Task 12: SSE growth — watermarks + the post-action nonce

**Files:** Modify `cockpit/api.py`; Test `tests/cockpit/test_api.py` (extend).

Extend `_change_token`: `trade_real=max(Trade.id)` (closes covered by the manual-close
ExitEvent), ★ `analysis=max(AnalysisRequest.id)|max(finished_at)|max(started_at)`
(claim sets started_at only; requeue nulls it — finished_at alone misses both),
`execution=max(ExecutionLog.id)`, `analyst=max(AnalystCall.id)|count(scored_at not
null)` (scoring is an UPDATE), `proposals`/`registry` file mtimes. ACTION NONCE: a
lock-guarded module counter bumped by every successful action POST, in the token
(single-process uvicorn — no multi-worker concern). All via `_watermark`. Tests: each
watermark moves on its write; nonce moves on POST. Commit:
`feat(cockpit): the wake token watches everything the six actions touch`

---

### Task 13: Frontend refactors — Segmented, seg- rename, Sparkline (zero behavior change)

**Files:** Create `cockpit-ui/src/components/Segmented.tsx`; Modify ★ `App.tsx`,
`Masthead.tsx`, `FacetToggle.tsx`, `PerformancePanel.tsx` (the five hand-rolled
segment instances live across these four files), `Sparkline.tsx` (viewBox + CSS width —
the fixed-pixel debt), `index.css` (`.mh-seg`/`.mh-spacer` → `.seg`/`.spacer` incl. the
`.perf-head .mh-seg` compound selectors).

**Steps:** extract `Segmented` (options/value/onChange/per-option title+disabled+
aria-pressed), swap all five call sites, rename, rescale Sparkline. `npm run lint &&
npm run build`, commit static SAME commit. Commit:
`refactor(cockpit-ui): Segmented component, seg- rename, responsive sparklines`

---

### Task 14: Frontend scaffold — screens, keyboard, api.ts contract, permanent polls

**Files:** Modify `cockpit-ui/src/lib/api.ts`, `App.tsx`, `index.css`.

**Steps:**
1. api.ts: types + fetchers for every Task 5–12 wire shape (mirror serializers exactly;
   tuple types; null contracts documented); `postAction` helper (X-Cockpit + {detail}
   errors).
2. Screens: `ScreenId` union (design numbering); App-level `screen` state; masthead +
   NeedsHandStrip + db-down card global; per-screen components mount only while
   visible. ★ Permanent App polls, explicitly: health, heartbeats, gate
   (+broker_configured), forward-books (the strip's source — it stays hoisted),
   attention. Mission Control keeps its grid and gains Zone slots (★ Tasks 16–20).
   `forward` = the Forward Books wall full-width; `systems` = heartbeat rail +
   playbook-integrity full-width.
3. Keyboard: one keydown effect — modifier + typing-context bail (INPUT/TEXTAREA/
   SELECT/isContentEditable) — '1'–'9' per the fixed numbering. Lands before any form.
4. lint+build+static. Commit:
   `feat(cockpit-ui): screens scaffold, 1-9 keys with typing guard, permanent-poll roster`

---

### Task 15: Frontend — Execution Safety screen + DISARM goes live

**Files:** Create `HoldToConfirm.tsx`, `CapGauge.tsx`, `BracketLamp.tsx`,
`screens/SafetyScreen.tsx`; Modify `Masthead.tsx`, `App.tsx`, `index.css`.

**Steps:** HoldToConfirm (900ms fill animation; ★ on hold-start fires dry-run; the
real POST arms only once the preview has LANDED — a completed hold waits on the
response, it never fires blind; Escape/release aborts). Masthead DISARM enabled IFF
`gate.broker_configured`; disabled tooltip "no broker configured"; caption verbatim
"cancels entry-side, keeps bracket stops". Result panel: cancelled/restored lists,
UNPROTECTED red and persistent until dismissed. SafetyScreen: preflight checklist
(go/no-go incl. the no-broker synthesized form), locks + caps mandate lamps, CapGauges
(R-label on loss; "no cap set" ≠ 0), bracket table, `env_scope` caption. Hard stale
line: safety force-nulls on fetch error (stale GO is worse than UNKNOWN). lint+build+
static. Commit: `feat(cockpit-ui): execution safety -- DISARM hold-to-confirm armed by
its own preview`

---

### Task 16: Frontend — Positions & Ledger screen + Zone B + the two trade forms

**Files:** Create `screens/PositionsScreen.tsx`, `LogTradeForm.tsx`,
`CloseTradeForm.tsx`, `RiskStrip.tsx`; Modify `App.tsx` (Zone B on Mission Control),
`index.css`.

**Steps:** RiskStrip: position strips (R at risk, distance-to-stop bar, BracketLamp,
badges), compact caps, "FLAT — no exposure" ONLY from a fresh fetch, "as of last
close" labels. PositionsScreen: open table (★ rows with null signal_id carry an
"unlinked (manual)" tag; override text renders only when non-null; live rows R-only
when dollar P/L is null), closed ledger + equity Sparkline, both forms. LogTradeForm:
prefilled from `/api/trade-defaults` (levels, size, actionability), live override
preview while editing (server recomputes authoritatively), "sizing unconfigured" on
shares==0. CloseTradeForm: exit price prefilled from the row's `last_close`, computed
R/$ preview, reason select. Both via `postAction`, local key-bump refetch; the nonce
covers other windows. lint+build+static. Commit:
`feat(cockpit-ui): positions & ledger -- zone B, engine-prefilled forms`

---

### Task 17: Frontend — Candidates screen + Zone D picks

**Files:** Create `screens/CandidatesScreen.tsx`, `PickCard.tsx`, `LevelRail.tsx`,
`ConvictionChip.tsx`; Modify `App.tsx` (Zone D), `index.css`.

**Steps:** Zone D: the surfaced ≤8 PickCards + flagged extras visually distinct
(never consuming the five) — actionability lamp (extended-is-normal styling for
reversals), LevelRail (floor/ceiling/stop/target + current close mark), cohort StatChip
(client-side join from the cohorts poll), ConvictionChip ("high · +0.32R (n=14)" /
"high · unproven (0 scored)" / absent), `is_repeat` tag, chart thumb via the proxy
(404 = no chart = normal), ONE row action: log-trade-prefilled. ★ Empty state:
"no picks today — next evening screen ~18:05 ET" (the Needs-Your-Hand empty-state
precedent). CandidatesScreen: the full flagged table (client-side toggles defaulting
to the digest view) + FunnelBar. lint+build+static. Commit:
`feat(cockpit-ui): candidates + zone D -- show-and-flag, one row action`

---

### Task 18: Frontend — Playbooks screen (TierChip, DeltaDiff, proposal actions)

**Files:** Create `screens/PlaybooksScreen.tsx`, `TierChip.tsx`, `DeltaDiff.tsx`;
Modify `App.tsx`, `index.css`.

**Steps:** Verdict table from the SIDECAR rows — TierChip (`forward_confirmed` = the
app's first saturated green; `replay_screened` amber+labeled; `hunch` gray; n=0 "empty
on this corpus"; bound labeled "Bonferroni-corrected lower"); md rendered as markdown,
Falsified section struck-through; drift lamp (amber advisory + missing-token tooltip);
reflection-due counter; proposals block: DeltaDiff rows (knob diff + gate verdict
inline + noop flag) with Approve/Withdraw (light HoldToConfirm 400ms, reason required;
approve success renders the three-artifact checklist + "uncommitted edit in edge/ —
commit it with your decision"). lint+build+static. Commit:
`feat(cockpit-ui): playbooks -- the first honest green, decisions with checklists`

---

### Task 19: Frontend — Analyst screen + analysis surface + ProvenancePopover

**Files:** Create `screens/AnalystScreen.tsx`, `AnalysisPanel.tsx`,
`ProvenancePopover.tsx`; Modify `StatChip.tsx` (popover hook), `Masthead.tsx` (spend
chip), `NeedsHandStrip.tsx` (unread analyses via localStorage vs
`attention.latest_analysis_id`), `App.tsx`, `index.css`.

**Steps:** ★ AnalysisPanel (the sixth action's surface — the blocker fix): request
form (ticker → POST /api/analysis), status list (queued/running/done/failed + stalled
badge + the worker-honesty copy), report viewer (summary md, chart thumbs, PDF link via
the proxies), empty state "no analysis requests yet"; the strip's unread item navigates
here. AnalystScreen: per-grade calibration (Stat-shaped mean R), nudge attribution,
unfilled fraction three-way split, calibration-progress countdown, spend today/7d/30d
+ undercount caveat, shadow-book label; masthead spend chip (the unrendered-field
debt). ProvenancePopover: click any StatChip → value/n/clusters/CI + bound type/cost
level/corpus/facet/unit. lint+build+static. Commit:
`feat(cockpit-ui): analyst + analysis surface + provenance on every number`

---

### Task 20: Frontend — Market Weather, Reference, Zone E ticker, strip growth

**Files:** Create `screens/WeatherScreen.tsx`, `screens/ReferenceScreen.tsx`,
`EventTicker.tsx`; Modify `App.tsx` (Zone E bottom strip), `NeedsHandStrip.tsx`,
`index.css`.

**Steps:** WeatherScreen: core line + alignment/flag chips, the labelled report split
on its pinned labels, indicator panel, `is_deep=False` badge, weekly-cadence caption,
★ empty state "no weekly report yet — runs Sundays ~09:00 ET". ReferenceScreen:
universe (+sector) + digest log + ★ the filterable exit log (three facets — the
Task 21 gate's surface). EventTicker (Zone E): two-row bottom strip, source-tagged,
★ empty state "no events yet". NeedsHandStrip grows: queued proposals ("· decide"),
approved-pending-promotion, reflection-due, unread analyses — all from the permanent
`attention` poll. lint+build+static. Commit:
`feat(cockpit-ui): weather, reference with exit log, zone E ticker, strip growth`

---

### Task 21: Delete the Streamlit dashboard

Only after Tasks 5–20 are green. ★ The deletion gate is `tests/dashboard/*` — the
executable behavior contracts being deleted (test_close_trade, test_trade_entry,
test_closed, test_exits, test_reference, test_analysis_page, test_app_smoke's
candidates/overview assertions): for EACH, name the cockpit test that now carries the
behavior; anything uncovered is a finding, not a shrug.

**Files:** Delete `src/swing_screener/dashboard/`, `tests/dashboard/`, `.streamlit/`;
Modify `pyproject.toml` (remove `streamlit>=1.38`; do NOT add altair — always
transitive), `.gitignore`, README.md, `docs/azure-deploy.md` (Step 5 → cockpit),
`docs/running-locally.md`, `docs/cockpit.md` (dashboard.md link), delete
`docs/dashboard.md`.

**Steps:** port-check table → delete → grep `swing_screener.dashboard` repo-wide
(content grep — the notify import was function-local) → zero code references → full
`pytest -q` → the Docker image sheds streamlit on the next CD build. Commit:
`feat(dashboard): delete -- the cockpit is the only instrument panel`

---

### Task 22: Docs + ship

**Files:** `docs/cockpit.md`, PR.

**Steps:** cockpit.md gains: the screens + `1–9` keys (★ ten screens; Reference via
the masthead link), the six actions (exact effects + guardrails — DISARM's venue
scope, approve's human checklist), ★ the corrected local-migration recovery
(`pip install -e ".[azure]"` → `alembic stamp <pre-phase3 head>` → `alembic upgrade
head`, or the documented one-line ALTER; ALSO retro-fix the existing stale-local.db
troubleshooting section which carries the same latent flaw), new endpoints table, env
vars, deferred list. Full gate: `pytest -q`, ruff, mypy, npm lint+build, static drift,
manual `--browser` walk of ★ all ten screens + a dry-run DISARM on the default
no-broker setup (expect the 409 path + a working safety screen, not a 500). Branch
`feat/cockpit-phase3`, PR `feat(cockpit): Phase 3 -- the cockpit is the whole
instrument panel`; body: scope decisions, review notes, Known posture (zero
forward_confirmed = no green yet; broker unset = UNKNOWN lamps; decisions are
working-tree edits; quotes are last-close; the local migration step). CI green;
Oliver merges.

---

## Explicit non-goals for Phase 3 (deferred with reasons)

Tray + native notifications (pystray absent; every useful behavior is a foreign-thread
WinForms call — the 2026-07-06 deadlock class; a future spike must demonstrate
run_detached + webview.start coexistence AND GUI-thread-safe show/hide, else abort);
PyInstaller stage 2 (design-optional; needs --add-data static/assets/universe_seed +
a new docs section); `/` command palette, `j/k` rows, AND Enter drill-in (drill-in
depends on row focus; zero scaffolding exists for any of the three); package-data for
static/ (recorded posture); auto-promotion of proposals or any config write from the
cockpit (North Star #1/#3); remote execution-mode flips from the UI (env is
per-process; the runbook owns it); SAS URLs (private container + proxies is the
posture); intraday quotes (daily-close engine); persisting full deep-analysis text
(lives in the PDF).

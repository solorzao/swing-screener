# On-demand Single-Ticker Deep Analysis — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement task-by-task.

**Goal:** Request a deep multi-timeframe Heiken-Ashi read of any ticker from the dashboard; a
cloud worker runs it, emails a PDF (like the daily digest), and the dashboard persists/shows
status + result.

**Architecture:** A new `analysis_requests` table is the queue. The local dashboard inserts a
`queued` row (it already writes to Azure SQL). A new scheduled worker CLI claims queued rows
atomically, reuses the pure single-ticker pieces (`fetch_bars` → `build_frames` →
`analyze_frames`/`analyze_reversals` → `render_chart`), runs ONE combined Opus MTF analyst call,
builds a report PDF (reusing pdf.py flowables), emails it (reusing the transport), uploads
artifacts to Blob, and marks the row `done`/`failed`. No Azure Function — the DB table is the queue.

**Tech Stack:** SQLAlchemy 2 + Alembic, anthropic (Opus 4.8 + web search), reportlab, ACS/SMTP
transport, Azure Blob, Streamlit 1.58, pytest. yfinance + Anthropic are always MOCKED in tests.

## Conventions
- Tests: `./.venv/Scripts/python -m pytest <path> -v`. Gates: `ruff check src tests alembic`, `mypy`.
- Branch `feat/on-demand-analysis` (design already committed). Conventional commits.
- Reuse the existing seams; do NOT duplicate analysis/pdf/email logic. Read the referenced files.

---

## Phase A — Data model & repo (the queue)

### Task 1: `AnalysisRequest` model + Alembic migration
**Files:** Modify `src/swing_screener/db/models.py`; Create a migration under `alembic/versions/`;
Test `tests/db/test_models_schema.py` (or a new `tests/db/test_analysis_requests.py`).

Add to models.py (mirror the existing model style; `datetime`/`date` already imported):
```python
class AnalysisRequest(Base):
    """Queue row for an on-demand single-ticker deep-analysis report."""

    __tablename__ = "analysis_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    requested_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued/running/done/failed
    recipient: Mapped[str] = mapped_column(String(256), default="")  # "" -> DIGEST_TO at send time
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    summary: Mapped[str] = mapped_column(String(512), default="")
    pdf_blob_key: Mapped[str | None] = mapped_column(String(512), default=None)
    chart_blob_keys: Mapped[str] = mapped_column(String(2048), default="")  # comma-joined keys
    error: Mapped[str | None] = mapped_column(String(1024), default=None)
```

**Migration:** read the most recent file in `alembic/versions/` to get the current head
`revision` id; create a new revision that chains from it (`down_revision = "<that head>"`) with
`op.create_table("analysis_requests", ...)` mirroring the columns above, and a `downgrade()` that
`op.drop_table("analysis_requests")`. (sqlite tests use `create_all`; Azure uses Alembic — the
migration must exist so `alembic upgrade head` creates the table in Azure.) Verify
`./.venv/Scripts/python -m alembic upgrade head` works against a temp sqlite URL, and that
`tests/test_alembic_offline.py` still passes.

**Test:** create the table via `get_engine("sqlite:///:memory:")`, insert an `AnalysisRequest`,
read it back, assert defaults (`status == "queued"`). Commit
`feat(db): add analysis_requests model + migration`.

### Task 2: repo helpers for the queue
**Files:** Modify `src/swing_screener/db/repo.py`; Test `tests/db/test_repo.py`.
Add (import `AnalysisRequest`, `datetime`; use `update()` for the atomic claim):
```python
def create_analysis_request(session, *, ticker, requested_at, recipient="") -> AnalysisRequest: ...
def list_analysis_requests(session, limit=50) -> list[AnalysisRequest]:  # newest first by requested_at
def get_analysis_request(session, request_id) -> AnalysisRequest | None
def claim_queued_requests(session, *, now, limit=10) -> list[AnalysisRequest]:
    """Atomically flip queued -> running and return the claimed rows.
    Use: select queued ids (limit), then UPDATE ... WHERE id IN (...) AND status='queued'
    SET status='running', started_at=now; commit; return the rows whose update stuck."""
def complete_analysis_request(session, request_id, *, summary, pdf_blob_key, chart_blob_keys, finished_at) -> None
def fail_analysis_request(session, request_id, *, error, finished_at) -> None
```
**Tests:** create→list (newest first); claim flips status to running and is idempotent (a second
claim returns nothing); complete/fail set the fields + finished_at. Commit
`feat(db): analysis-request queue repo helpers`.

---

## Phase B — Analysis core

### Task 3: per-timeframe deterministic read (pure)
**Files:** Create `src/swing_screener/notify/ticker_report.py`; Test `tests/notify/test_ticker_report.py`.
Define dataclasses + a pure builder. Read `pipeline/analyze.py` for `SignalResult`'s fields
(`.timeframe, .score, .play_type, .strength, .entry_floor, .entry_ceiling, .stop, .target, .atr,
.rsi, .trigger_close, .mtf_aligned, .quality_tier, .volatility_tier, .oversold, .frame, .ctx,
.zone`) and `build_frames`/`analyze_frames`/`analyze_reversals` signatures.
```python
@dataclass(frozen=True)
class TimeframeRead:
    timeframe: str
    ha_trend: str          # "bullish"/"bearish" from the last HA candle
    ema_aligned: bool      # EMA20 > EMA50 (up) — derive from the frame
    rsi: float
    atr_pct: float
    setup: SignalResult | None   # the firing signal on this TF, if any
    chart_path: str | None = None

@dataclass(frozen=True)
class TickerReport:
    ticker: str
    name: str
    run_at: datetime
    reads: list[TimeframeRead]   # 4h, 1d, 1wk, 1mo order
    summary: str = ""            # filled by the analyst (overall stance / core)
    analysis_text: str = ""      # filled by the analyst (full narrative)
    is_deep: bool = False
```
`build_ticker_reads(ticker, frames, cfg) -> list[TimeframeRead]`: for each timeframe present in
`frames` (ordered `["4h","1d","1wk","1mo"]`), compute ha_trend/ema_aligned/rsi/atr_pct from the
enriched frame's last row, and attach the firing signal from
`analyze_frames(ticker, {tf: frame}, cfg) + analyze_reversals(...)` (first match for that tf, if any).
**Test:** with a hand-built enriched frame (use the `bars`/`make_bars` fixture + `build_frames`),
assert ha_trend/ema_aligned/rsi are read correctly and that a firing-setup frame attaches a setup
while a flat frame leaves `setup=None`. Commit `feat(notify): per-timeframe deterministic ticker read`.

### Task 4: `analyze_ticker_deep` — one Opus MTF call
**Files:** Modify `src/swing_screener/notify/analysis.py`; Test `tests/notify/test_analysis_ticker.py`.
Reuse the existing helpers in analysis.py: `_create_message`, `_extract_text_and_citations`,
`_format_sources`, `_REASONING_EFFORT`, `_REASONING_MAX_TOKENS`, the `client or anthropic.Anthropic(...)`
seam, and base64 image content (see `_deep_user_content`). Add a new MTF system prompt + builder:
```python
def analyze_ticker_deep(report: "TickerReport", *, charts: list[bytes] | None = None,
                        context_text: str = "", client=None, model="claude-opus-4-8",
                        reasoning="high", max_searches=4, web_search=True) -> tuple[str, str, bool]:
    """Return (summary, analysis_text, is_deep). One Opus call over the WHOLE multi-timeframe
    picture: deterministic per-TF facts + the charts. Falls back to a deterministic multi-TF
    summary on ANY failure (so the worker still emails a report)."""
```
- System prompt (new constant `_TICKER_SYSTEM`): analyst for a swing trader; given a ticker's
  per-timeframe HA facts (treat any firing setup's levels as ground truth, never invent levels)
  and charts; web_search for sentiment/sector; reply EXACTLY as labelled lines:
  `CORE: <one-line overall stance>`, then `4h:/1d:/1wk:/1mo:` lines, then `Setups:`, `Risk:`,
  `Watch:` (key levels). Same anti-preamble discipline as `_DEEP_SYSTEM`.
- User content: the 4 chart images FIRST (reuse the base64 image dict shape), then a text block
  listing each TimeframeRead's facts + any firing setup's entry/stop/target.
- Parse: first `CORE:` line → summary; full text → analysis_text; append `_format_sources(sources)`.
  On exception, build a deterministic summary/text from the reads (no narrative) and `is_deep=False`.
**Tests (fake client, no network):** a fake `client.messages.create` returning a canned labelled
reply → assert summary is the CORE line, analysis_text contains the per-TF lines, is_deep True; a
fake that raises → deterministic fallback, is_deep False. Commit
`feat(notify): analyze_ticker_deep — combined Opus multi-timeframe analyst`.

---

## Phase C — Report output (PDF + email)

### Task 5: `build_ticker_report_pdf`
**Files:** Modify `src/swing_screener/notify/pdf.py`; Test `tests/notify/test_pdf.py`.
Reuse `_chart_image`, `_rationale_flowables`, and the blob/local chart-resolution pattern from
`build_story`. New function:
```python
def build_ticker_report_pdf(report: "TickerReport", out_path: Path) -> Path:
    """One ticker, multi-timeframe: header (ticker · name · run_at), an overall-stance paragraph
    (report.summary), then a section per TimeframeRead (chart + a state/levels table + the per-TF
    note sliced from report.analysis_text), built from report.analysis_text via _rationale_flowables."""
```
Per-TF section: heading `f"{tf} — {ha_trend}, RSI {rsi:.0f}, ATR {atr_pct:.1%}"`; the chart
(resolve via the same `blob_enabled()`/`download_bytes` vs local-path logic as `build_story`);
a small levels table if `read.setup` is not None (entry zone/stop/target/R:R) else a
"No setup firing" line; then the relevant analysis lines. Keep it a SimpleDocTemplate(letter).
**Test:** build a `TickerReport` with 2 reads (one with a setup, one without) → call the function
with a `tmp_path` out file → assert the PDF file exists and is non-empty (smoke). Also unit-test a
`build_ticker_story(report)` factored-out helper returns flowables without parsing the PDF. Commit
`feat(notify): build_ticker_report_pdf`.

### Task 6: `compose_ticker_report_body` + email send
**Files:** Modify `src/swing_screener/notify/body.py`; Test `tests/notify/test_body.py`.
Read `body.py` for `compose_digest_body`'s return type (an `EmailContent`-like (subject, text,
html) — match it) and `transport.py` for `resolve_sender()` + `send(to, subject, text, html,
attachments)`. Add:
```python
def compose_ticker_report_body(report: "TickerReport") -> <same EmailContent type>:
    """Subject 'Swing Screener — Deep Read: AAPL (Jun 16)'; text+HTML = the summary + per-TF
    one-liners + 'Full report attached (PDF).'"""
```
**Test:** assert the subject contains the ticker + a date and the text contains the summary. Commit
`feat(notify): compose_ticker_report_body`.

---

## Phase D — Worker / CLI

### Task 7: the worker + CLI entry point
**Files:** Create `src/swing_screener/notify/ondemand.py`; Test `tests/notify/test_ondemand.py`.
Read `pipeline/run.py` for the CLI/engine/migrate pattern (`_resolve_db_url`, `_migrate_with_retry`,
`get_engine`, `argparse` `main()`), `_fetch_all_timeframes` + `_render_and_attach` for the
single-ticker fetch+chart path, `storage/blob.py` for `blob_enabled`/`upload_chart`/`download_bytes`
(+ a blob upload for the PDF — reuse the chart upload helper or add `upload_bytes(key, data)` if none
exists; check blob.py first), and `data/universe.py` `names_by_tracker`/`names_by_ticker` for the name.
```python
def process_one(session, request, *, cfg, cache_dir, chart_dir, settings, today, client=None,
                sender=None) -> None:
    """Fetch 4 TFs, build_frames, build_ticker_reads, render per-TF charts, upload them, run
    analyze_ticker_deep, build the PDF, upload it, send the email (idempotent via EmailLog
    kind='ondemand' alert_key=str(request.id)), then complete_analysis_request. On ANY exception:
    fail_analysis_request(error=...) and log — never raise (one bad request can't break the batch)."""

def process_pending(session, *, settings, now, cfg=None, client=None, sender=None, limit=10) -> int:
    """claim_queued_requests then process_one for each; return count processed."""

def main() -> None:  # argparse like pipeline/run.py: --db, --cache-dir, --chart-dir; migrate for mssql
```
Recipient: `request.recipient or get_secret("DIGEST_TO")`. On-demand ALWAYS runs deep
(`analyze_ticker_deep(... web_search=True, reasoning=settings.analysis_reasoning,
model=settings.analysis_model, max_searches=settings.analysis_max_searches)`) — it does NOT check
`deep_analysis_enabled`.
**Tests (offline):** monkeypatch `ondemand._fetch_all_timeframes` (or reuse run's) to return a
firing daily frame; inject a fake anthropic client + a fake sender (records calls); seed a `queued`
request; `process_pending(...)` → assert the request is `done`, summary set, the fake sender got one
email with a PDF attachment, and a second `process_pending` is a no-op (already claimed/done).
Add an isolation test: a ticker whose fetch raises → request `failed` with an error, batch returns. Commit
`feat(notify): on-demand analysis worker + CLI`.

---

## Phase E — Dashboard

### Task 8: "Deep Analysis" page
**Files:** Modify `src/swing_screener/dashboard/app.py`; Test `tests/dashboard/test_analysis_page.py`.
Add `_render_analysis(session)` and register it in `PAGES` (e.g. after "Today's Candidates", group
"Signals", or a new "Analysis" position). Pattern after `_render_entry` (form) + `_render_universe`
(table) + the candidates chart-from-blob resolution (`_resolve_chart_image`):
- `ui.page_header("Deep Analysis", caption="Request an in-depth multi-timeframe read; you'll get an email + it shows here.")`
- Form: `ticker = st.text_input(...)`; submit → validate non-empty → `repo.create_analysis_request(session, ticker=ticker.strip().upper(), requested_at=<now>)` → `st.success(...)` → `st.rerun()`. (For `now`, use `datetime.now()` — app.py may need the import; it's a real runtime value, fine outside tests.)
- A **Refresh** button (`st.button` → `st.rerun()`).
- Requests table: `repo.list_analysis_requests(session)` → a DataFrame (status badge, ticker,
  requested_at, summary). Status → emoji via a small local map.
- Selected done request (a `st.selectbox` of done ids, or a row select): show `summary`, render each
  chart key via `_resolve_chart_image`, and a **Download PDF** button:
  `st.download_button("Download PDF", data=download_bytes(req.pdf_blob_key), file_name=...)` guarded
  for missing/local. Failed → `st.expander` with `req.error`.
**Tests (AppTest):** seed a `done` AnalysisRequest (monkeypatch `download_bytes` to return fake PDF
bytes); switch to the page; assert no exception, the ticker/summary surface, and submitting the form
creates a new `queued` row (assert via a fresh Session). Update the smoke `ALL_PAGES` list in
`test_app_smoke.py` to include the new page. Commit `feat(dashboard): on-demand Deep Analysis page`.

---

## Phase F — Infra & docs

### Task 9: infra job + CD loop
**Files:** Modify `infra/modules/jobs.bicep`; Modify `.github/workflows/cd.yml`; (read `infra/main.bicep`
for how jobs are declared + parameterized).
- Add an `on-demand-analysis` Container Apps Job mirroring `daily-digest` (same image, identity, env,
  Key Vault refs) with cron `*/15 * * * *` and command
  `python -m swing_screener.notify.ondemand`. Follow the existing job module's shape exactly.
- Add `on-demand-analysis` to the `for job in ...` loop in `cd.yml` (so CD repoints its image too).
- Verify `az bicep build --file infra/main.bicep` compiles (no deploy). Commit
  `feat(infra): on-demand-analysis scheduled job (+ CD image update)`.

### Task 10: docs + final verification + PR
- `docs/dashboard.md`: add the Deep Analysis page to the nav list. `docs/azure-deploy.md`: note the
  new job + that it (like the others) needs the one-time `az deployment sub create` to be created,
  after which CD keeps its image current; the `analysis_requests` table is created by
  `alembic upgrade head` on the job's first run.
- Full `./.venv/Scripts/python -m pytest -q`, `ruff check src tests alembic`, `mypy` — all clean.
- Optional offline end-to-end: `process_pending` against a local sqlite with a fake client + fake
  sender (already covered by Task 7).
- Open a PR against main.

## Risks / notes
- **Opus MTF prompt** is new — keep the "never invent levels / no preamble" discipline from `_DEEP_SYSTEM`.
- **Blob PDF upload:** if `storage/blob.py` lacks a generic `upload_bytes`, add one mirroring
  `upload_chart`; check first (Task 7).
- **Atomic claim** must be a single UPDATE…WHERE status='queued' (portable on sqlite + SQL Server);
  avoid `.is_()` on booleans per the repo's existing SQL-Server note.
- **Cost:** one Opus call + ≤4 web searches + 4 charts per request; user-initiated, low volume.
- **app.py `datetime.now()`** is fine at runtime (the no-`Date.now` rule is a workflow-script
  constraint, not a app constraint); tests seed explicit timestamps.

# On-demand single-ticker deep analysis — design

**Date:** 2026-06-16
**Status:** Approved (brainstorming)
**Scope:** Request an in-depth, multi-timeframe Heiken-Ashi deep read of any ticker on
demand; a cloud worker runs it, emails a PDF (like the daily digest), and the dashboard
persists/shows the result.

## Goal

Let the user request a deep read of a specific ticker from the dashboard. A cloud job runs
the same deep-analysis + PDF + email plumbing as the daily digest, emails the report, and
the dashboard shows status and the persisted result.

**Success criteria:** request a ticker → within ~15 min an email with a 4-timeframe PDF
arrives → the dashboard shows the request `done` with summary, charts, and a PDF download.

## Decisions (from brainstorming)

- **Execution:** cloud-queued. The local dashboard writes a request row to Azure SQL; a new
  scheduled Container Apps job processes it. No Azure Function / queue service needed.
- **Report scope:** full multi-timeframe HA read (4h / 1d / 1wk / 1mo) — trend state +
  setup-if-firing + "what would trigger a setup" when nothing's firing. Charts per timeframe.
- **Analysis:** one combined Opus MTF call per report; on-demand always runs the full deep
  treatment (bypasses the `deep_analysis_enabled` daily gate).
- **Cadence:** worker runs every ~15 min (knob).
- **One ticker per request**, recipient defaults to `DIGEST_TO`.

## 1. Data model & request lifecycle

New `analysis_requests` table (the queue — the dashboard already writes to Azure SQL):

| column | meaning |
|---|---|
| `id`, `ticker`, `requested_at` | the ask |
| `status` | `queued` → `running` → `done` / `failed` |
| `started_at`, `finished_at` | timing |
| `summary` | one-line core read (shown in UI) |
| `pdf_blob_key`, `chart_blob_keys` | report artifacts in Blob |
| `recipient` | defaults to `DIGEST_TO` |
| `error` | failure detail |

**Flow:** dashboard inserts `queued` → worker claims atomically
(`UPDATE … WHERE status='queued'`, so concurrent replicas never double-run) → runs analysis
→ uploads PDF + charts to Blob → writes `summary` → emails the PDF → marks `done` (or
`failed` + `error`). Dashboard polls on rerun; `done` rows expose summary, charts, PDF
download. Idempotency reuses `EmailLog` (`kind="ondemand"`, `alert_key=request id`) so a
retried job never double-emails.

## 2. Analysis core (single ticker, 4 timeframes)

Reuses the pure nightly pieces for one ticker:
1. `fetch_bars` (1h + 1d) → resample to 4h/1wk/1mo → `build_frames` (HA, EMA20/50, ATR, RSI).
2. Per-timeframe deterministic read: HA trend (color/run), EMA20-vs-50 alignment, RSI, ATR%,
   distance to nearest swing high/low; run `analyze_frames` + `analyze_reversals` and attach
   the firing setup's entry zone / stop / target when present.
3. One combined `analyze_ticker_deep(ticker, tf_reads, charts, context)` Opus call (web search
   on) → structured read: overall stance, per-timeframe notes, firing setup(s) with levels (or
   "no setup — state + what would trigger one"), key levels to watch. Reuses the analyst's
   guardrails (narrate facts, never invent levels), the reasoning-budget knob, and the
   deterministic fallback on API failure.

One MTF call (not four) — more coherent and cheaper: 1 Opus call + ≤4 web searches + 4 charts
per report.

## 3. Report output: PDF + email

New `build_ticker_report_pdf(report)` reuses pdf.py's flowable helpers (`_chart_image`, the
levels table, the rationale formatter) with an on-demand layout: header (ticker · name ·
timestamp) → overall stance → per-timeframe sections (chart + state line + setup levels if
firing + narrative note) → key-levels/what-to-watch footer.

Email reuses the transport untouched: `compose_ticker_report_body(report)` (subject
*"Swing Screener — Deep Read: AAPL (Jun 16)"*, text+HTML summary, PDF attached) →
`resolve_sender().send(to, …, attachments=[pdf])`, to `recipient`/`DIGEST_TO`. The worker
uploads the PDF + 4 charts to Blob for the dashboard.

## 4. Dashboard: a new "Deep Analysis" page

Added to the `PAGES` registry, inside the error boundary:
- **Request form** — ticker input + "Request report" → inserts a `queued` row (local
  dashboard → Azure SQL, like Trade Entry). Validates/uppercases the ticker.
- **Requests table** — recent requests with status badges (🕓 queued · ⏳ running · ✅ done ·
  ⚠️ failed), timestamp, ticker, summary for done. A **Refresh** button re-polls.
- **Selected report** — done: summary, the 4 charts (Blob bytes, like Today's Candidates), a
  **Download PDF** button (`st.download_button` over Blob bytes). Failed: error in an expander.

## 5. Config, failure handling, infra, testing

- **Config/secrets:** reuses `ANTHROPIC_API_KEY` (Key Vault), `DIGEST_TO`, the ACS/SMTP
  transport, Blob — nothing new to provision. On-demand bypasses `deep_analysis_enabled` but
  reuses `analysis_model` / `analysis_reasoning` / `analysis_max_searches`.
- **Failure isolation:** each request independent (one `failed` row never blocks others); if
  Opus fails, fall back to the deterministic MTF read — still build/email the PDF, mark `done`
  with an "analysis degraded" note. Atomic claim + `EmailLog` key prevent double-process /
  double-email.
- **Infra:** new Container Apps Job `on-demand-analysis` in `infra/modules/jobs.bicep` (cron
  `*/15`, same image + managed identity + existing roles); new CLI
  `python -m swing_screener.notify.ondemand`; Alembic migration for `analysis_requests`.
- **Testing (offline):** mocked Anthropic client + mocked `fetch_bars`; unit tests for the
  per-timeframe deterministic read, the request repo (insert/claim/complete/fail), the worker
  (claims → processes → done; isolation; idempotent email), a PDF-builder smoke test, the body
  composer, and the dashboard form + table via `AppTest`.

## Out of scope (YAGNI)

Batch/multi-ticker requests; an HTTP trigger function; live auto-polling in the UI; historical
re-analysis; configurable per-request reasoning depth.

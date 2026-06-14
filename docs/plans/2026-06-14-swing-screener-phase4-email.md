# Swing Screener — Phase 4 (Email Digests + Claude Analysis + PDF) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task, test-first per superpowers:test-driven-development. When implementing the PDF builder (Task 3) use the **anthropic-skills:pdf** skill; for the Anthropic client (Task 1) the **claude-api** skill is the reference.

**Goal:** After the nightly run, send a concise **summary email** of the day's top picks plus a detailed **PDF attachment** (per-pick analysis, annotated chart, levels, trade type, R:R) — daily (top 5), weekly, and monthly — with Claude-written rationale, plus urgent exit alerts.

**Architecture:** Keep everything testable and offline-in-tests: a mockable **Anthropic client seam** for the rationale, **pure composers** for the email body, a **PDF builder** over the charts the pipeline already renders, and a **mockable SMTP sender**. A digest orchestrator wires them, degrades gracefully (LLM down → facts-only; PDF fails → summary email without attachment), and is idempotent via the existing `email_log`.

**Tech Stack:** Python 3.12; `anthropic` SDK (model **`claude-sonnet-4-6`**, upgradeable to `claude-opus-4-8`); `reportlab` (via the pdf skill) for PDFs; stdlib `smtplib`/`email` for Gmail SMTP. SQLAlchemy/SQLite (existing).

**Design reference:** `docs/plans/2026-06-14-swing-screener-design.md` (the email = summary + PDF section).
**Builds on:** Phases 1–3 on `main` (signals, charts, repo, analyze).

---

## Conventions

- New deps in `pyproject.toml`: `anthropic`, `reportlab`. Secrets from env (later Key Vault): `ANTHROPIC_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `DIGEST_TO`.
- **Tests never hit the network or send mail.** Mock the Anthropic client (`analysis._client` seam) and `smtplib.SMTP`. PDF tests assert a non-empty file (no pixel assertions).
- New package: `src/swing_screener/notify/` (analysis, email body, pdf, smtp, digests, run). Each piece small and tested; the orchestrator is the only thing wiring I/O.
- Each task: failing test → run (fail) → minimal impl → run (pass) → full gate (`ruff`/`mypy`/`pytest -q`) → commit. Branch `phase4-email`; PR at the end.

---

## Task 1: Claude analysis client (mockable, graceful)

**Files:** `pyproject.toml` (add `anthropic`); `src/swing_screener/notify/__init__.py`; `src/swing_screener/notify/analysis.py`; `tests/notify/__init__.py`; `tests/notify/test_analysis.py`.

`SignalAnalysis` (frozen: `core_reason: str`, `rationale: str`). `analyze_signal(facts: SignalFacts, *, client=None) -> SignalAnalysis` where `SignalFacts` is built from a `Signal`/`SignalResult` (ticker, timeframe, horizon/trade-type, score, mtf, tags, trigger_close, atr, rsi, entry zone, stop, target, R:R). It calls Claude to narrate the deterministic facts into a tight rationale + a one-line core reason; the model **only narrates** — it never invents a setup.

- Use the official SDK: `client = client or anthropic.Anthropic()`; `client.messages.create(model="claude-sonnet-4-6", max_tokens=600, system=SYSTEM, messages=[{"role":"user","content": _facts_prompt(facts)}])`; extract the first text block. (No thinking needed for short narration; keep `max_tokens` modest.)
- **Graceful degradation:** wrap the call in try/except; on ANY failure return a deterministic fallback `SignalAnalysis(core_reason=_deterministic_reason(facts), rationale=_deterministic_rationale(facts))` (built from the facts) and log a warning. The pipeline must never block on the LLM.

**Tests** (mock the client): a fake client returning a canned message → assert the rationale/core_reason are parsed from it; a fake client that raises → assert the deterministic fallback is returned (non-empty, mentions the ticker). No network. Commit: `feat: claude analysis client with graceful fallback`.

## Task 2: Email body composer (pure)

**Files:** `src/swing_screener/notify/body.py`; Test `tests/notify/test_body.py`.

Pure functions building the **scannable summary** body (text + minimal HTML):
- `compose_digest_body(kind, picks, exit_alerts, *, has_pdf) -> EmailContent(subject, text, html)` where each pick row = ticker · **trade type** (short/medium/long) · core one-line reason; an exit-alerts section (🔴/🟠/🟡) if any; and a "details in the attached PDF" pointer when `has_pdf`.
- `kind` ∈ {"daily","weekly","monthly"} drives the subject (e.g. "Swing Screener — Daily Top 5 (2026-06-15)").

**Tests:** given picks + alerts, assert the subject, that every ticker + trade type + reason appears, the PDF pointer toggles with `has_pdf`, and empty picks yields a "no setups today" body. Pure, no I/O. Commit: `feat: digest email body composer`.

## Task 3: Detailed PDF builder (uses the pdf skill)

**Files:** `src/swing_screener/notify/pdf.py`; Test `tests/notify/test_pdf.py`.

`build_digest_pdf(picks, out_path) -> Path` — ONE PDF per digest, a **section per pick**: heading (ticker · trade type · score), the annotated chart image (`pick.chart_path` if present), a levels table (entry zone, stop, target, R:R), the category tags (quality/volatility/oversold) + MTF, and the Claude rationale paragraph. Use `reportlab` (Platypus `SimpleDocTemplate` + `Paragraph`/`Image`/`Table`) — consult the **anthropic-skills:pdf** skill for the construction pattern. Missing chart → render the section without the image (don't crash).

**Tests:** build a PDF for 2 sample picks (one with a tiny generated PNG, one without) to `tmp_path`; assert the file exists, is non-empty, and starts with the `%PDF` magic bytes. No pixel/text assertions. Commit: `feat: detailed digest PDF builder`.

## Task 4: Gmail SMTP sender (mockable)

**Files:** `src/swing_screener/notify/smtp.py`; Test `tests/notify/test_smtp.py`.

`send_email(*, to, subject, text, html=None, attachments=(), sender=None, password=None) -> None` builds a `EmailMessage` (plain + optional HTML alternative; attaches PDFs by path/bytes) and sends via `smtplib.SMTP_SSL("smtp.gmail.com", 465)` with `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` (args override env). 

**Tests:** monkeypatch `smtplib.SMTP_SSL` with a fake that records `login`/`send_message`; assert credentials used, the message has the subject/recipient, and an attachment is present. No real connection. Commit: `feat: gmail smtp sender`.

## Task 5: Digest selection from the DB

**Files:** `src/swing_screener/notify/select.py`; Test `tests/notify/test_select.py`.

Pure-ish queries over the store:
- `daily_picks(session, run_date, *, top_n=5) -> list[Signal]` — top-N by rank for the run date.
- `weekly_picks(session, run_date, *, top_n=5)` / `monthly_picks(...)` — same but filtered to `timeframe == "1wk"` / `"1mo"`.
- `pending_exit_alerts(session, run_date) -> list[ExitEvent]` — that day's exit events (for real trades), newest first.

**Tests** (temp SQLite, seeded signals/exit_events): assert daily returns ≤ top_n ordered by rank; weekly/monthly filter by timeframe; exit alerts come back for the date. Commit: `feat: digest selection queries`.

## Task 6: Exit-alert email (concise, no PDF)

**Files:** `src/swing_screener/notify/alerts.py`; Test `tests/notify/test_alerts.py`.

`compose_exit_alert(events) -> EmailContent` — a concise, urgent body (per event: ticker · tier · reason · message), no attachment. (Sent via Task 4's `send_email`.) Hard-stop (🔴) events get an "URGENT" subject.

**Tests:** given a hard-stop + an advisory event, assert subject urgency and that both surface in the body. Pure. Commit: `feat: exit-alert email composer`.

## Task 7: Digest orchestrator (CLI)

**Files:** `src/swing_screener/notify/run.py` (CLI: `python -m swing_screener.notify.run --kind daily`); Test `tests/notify/test_run.py`.

`send_digest(*, kind, db_url, run_date=None, anthropic_client=None, smtp_send=smtp.send_email, today=None) -> DigestResult(n_picks, pdf_attached, sent)`:
1. Select picks (Task 5) + exit alerts.
2. For each pick, `analyze_signal` (Task 1) — graceful fallback baked in.
3. `build_digest_pdf` (Task 3) → on failure, log + continue with `pdf_attached=False`.
4. `compose_digest_body` (Task 2) → `smtp_send(... attachments=[pdf] if pdf else [])`.
5. If exit alerts exist, also send the exit-alert email (Task 6).
6. Record an `EmailLog` row and **skip if already sent for (kind, run_date)** (idempotency).

Inject the Anthropic client and the SMTP-send function so the test passes fakes (no network/mail). **Test:** seed a temp DB with signals, run `send_digest(kind="daily", anthropic_client=<fake>, smtp_send=<recorder>)`; assert the recorder captured one email with a subject + a PDF attachment, the body mentions the top ticker, and a second run is a no-op (idempotent). Commit: `feat: digest orchestrator`.

## Task 8: Docs + finalize

- `docs/email-digests.md`: env vars (`ANTHROPIC_API_KEY`, Gmail app-password setup, `DIGEST_TO`), the three cadences, how it wires into the nightly run (the pre-open job sends the daily digest), and the graceful-degradation behavior.
- README roadmap → Phase 4 done; link the doc.
- Full gate; final holistic review; push `phase4-email`; CI green; open PR into `main`.

---

## Testing notes

- **No network / no mail in tests.** Mock the Anthropic client and `smtplib`; the orchestrator takes both as injectable seams. PDF tests assert file creation + `%PDF` header only.
- **Model:** `claude-sonnet-4-6` (per the design — cheap, sufficient for narrating facts). Swap to `claude-opus-4-8` by changing one constant if you want richer write-ups. The model **narrates deterministic facts**; the system prompt forbids inventing setups.
- **Secrets** are read from env now; Phase 5 moves them to Azure Key Vault. Never commit a key.

## Deferred

- Scheduling the three cadences in the cloud (Phase 5 — Azure Container Apps Jobs cron). Locally, run `python -m swing_screener.notify.run --kind daily` after the pipeline (or wire it into a local scheduled task).
- Inline chart thumbnail in the email body (kept text-only by design; revisit if wanted).

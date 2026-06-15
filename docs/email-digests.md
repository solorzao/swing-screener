# Email digests (Phase 4)

After the nightly run, the screener sends a **concise summary email** of the day's top
picks plus a **detailed PDF attachment** — daily (top 5), weekly, and monthly — with a
Claude-written rationale per pick, and fires urgent **exit alerts** for active trades.

- **Email body** = a quick scan: per pick, ticker · **trade type** (short / medium / long) ·
  the core one-line reason, plus any exit alerts and a "details attached" pointer.
- **Attached PDF** = one document per digest, a section per pick: the annotated Heiken Ashi
  chart, the levels table (entry zone, stop, target, R:R), the category tags + MTF, and the
  full Claude rationale.
- **Exit alerts** go out as concise, urgent standalone emails (no PDF — a hard stop shouldn't
  wait on PDF rendering).

## Configuration (environment variables)

The email **transport is pluggable** (`notify/transport.py`): if `SWING_ACS_ENDPOINT` is set
it sends via **Azure Communication Services** authenticated by the managed identity (no
stored credential — this is what the Azure deploy uses); otherwise it falls back to Gmail
SMTP for local use.

| Var | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key (model `claude-sonnet-4-6`; upgradeable to `claude-opus-4-8`) |
| `DIGEST_TO` | the recipient address (usually yourself) |
| `SWING_ACS_ENDPOINT` / `SWING_ACS_SENDER` | *(cloud)* ACS endpoint + verified MailFrom address; when set, email sends via ACS + managed identity, no secret stored |
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` | *(local fallback only)* a Gmail account + **app password** (not your login password) — used only when ACS isn't configured |

## Run it

```powershell
$env:ANTHROPIC_API_KEY = "<your-anthropic-api-key>"
$env:GMAIL_ADDRESS = "you@gmail.com"
$env:GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"
$env:DIGEST_TO = "you@gmail.com"

.\.venv\Scripts\python -m swing_screener.notify.run --kind daily --db sqlite:///local.db
```

`--kind` is `daily` (top 5 overall), `weekly` (weekly-timeframe plays), or `monthly`. The PDF
is written to `--pdf-dir` (default `.digests`). Re-running the same `(kind, run_date)` is a
**no-op** (idempotent via `email_log`), so a retry won't double-send.

### Where it fits in the nightly cadence

- **Daily** — the pre-open job (after the evening screen) sends the daily top-5 digest.
- **Weekly** — run `--kind weekly` once a week (e.g. after Friday's close).
- **Monthly** — run `--kind monthly` once a month.
- **Exit alerts** — sent alongside whichever digest run detects them.

Locally these are manual / a scheduled task; Phase 5 moves them to Azure Container Apps Jobs
cron.

## Graceful degradation

The digest never blocks on a failure:
- **Claude API down** → each pick falls back to a deterministic rationale built from the facts
  (the summary email + PDF still send, just without the LLM prose).
- **PDF build fails** → the summary email still sends, without the attachment.

## Notes

- The model **narrates the deterministic facts** the engine computed — the system prompt
  forbids inventing setups or levels. To switch to richer write-ups, change the model constant
  in `notify/analysis.py` to `claude-opus-4-8`.
- Secrets are resolved by `config_secrets.get_secret`: **environment variables locally**, and
  in Azure from **Key Vault** (when `KEY_VAULT_URL` is set) via the job's managed identity — no
  stored credentials, and secret **values are never logged**. Never commit a key. See the
  [Azure deploy runbook](azure-deploy.md).
- The schema is owned by **Alembic** (`alembic upgrade head`); the pipeline self-migrates on
  mssql startup. A `local.db` created before Phase 4 predates the `email_log.run_date`/`alert_key`
  columns — recreate it (or `alembic upgrade head` against it) before the first digest run.

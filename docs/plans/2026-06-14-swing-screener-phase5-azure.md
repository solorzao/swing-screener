# Swing Screener — Phase 5 (Azure Deploy + CD) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task, test-first per superpowers:test-driven-development for every CODE task. For the infra tasks (Dockerfile, Bicep, Jobs, Key Vault, CD) there are no unit tests — follow the exact commands and verify as written. Consult **context7** for current `az containerapp job`, Bicep, `azure-storage-blob`, `azure-identity`, and Alembic syntax before writing each infra file rather than relying on memory. Several Azure role-definition IDs and the `Container Apps Jobs` role name MUST be re-verified against live `az role definition list` output (see Task 13/15) — do not trust the IDs inline here without confirming.

**Goal:** Deploy the existing nightly engine to Azure — one container image run as scheduled Container Apps Jobs against Azure SQL, charts in private Blob Storage, secrets in Key Vault via a user-assigned managed identity (no stored creds anywhere) — with CD that auto-builds and repoints the jobs on merge to `main` (CI-gated via OIDC, no long-lived credentials). The dashboard **stays local**, pointed at Azure SQL via `az login`.

**Architecture:** Small, seam-based code changes first, infra second. Code: bound every string column (so Azure SQL can index `ticker`) and add an `EmailLog` unique constraint (so idempotency is concurrency-safe); a **real-trade exit-check entrypoint** that actually writes `is_paper=False` `ExitEvent` rows (the hourly exit job has nothing to run today — see Task 3); a **finer exit-alert idempotency key** so the intraday cadence can send more than one alert per day; `get_engine` learns a passwordless `mssql+pyodbc` branch (no `create_all` — Alembic owns the schema); a settings module (absolute paths); a centralized env-first / Key-Vault-fallback secrets resolver; a pluggable private blob chart store with the **exact** key string persisted on `Signal.chart_path` (and the `.exists()` read-guards replaced on the blob branch); Alembic owning the Azure SQL schema; UTC timestamps. Then infra: one Dockerfile (digest-pinned base, ODBC Driver 18 + matplotlib/reportlab native libs), Bicep modules (ACR + Container Apps env + Jobs + Azure SQL + Storage + Key Vault + one shared user-assigned managed identity + Log Analytics with a daily cap), a deploy runbook, and last an OIDC CD workflow. DST and "last business day" are solved in Python (a `zoneinfo` Eastern gate at the container entrypoint), because ACA cron is UTC-only.

**Tech Stack:** Python 3.12; `pyodbc` + Microsoft ODBC Driver 18 for SQL Server; `azure-identity`, `azure-keyvault-secrets`, `azure-storage-blob`; `alembic`; `DefaultAzureCredential` (user-assigned managed identity in-cloud, `az login` locally). Azure: Container Registry (Basic), Container Apps environment + Jobs (Schedule trigger), **Azure SQL Basic DTU by default** (GP serverless is a documented one-line opt-in), Storage (Standard_LRS, one private container), Key Vault (RBAC), one user-assigned managed identity, Log Analytics (capped). IaC in Bicep; CD via GitHub Actions + Azure OIDC.

**Design reference:** `docs/plans/2026-06-14-swing-screener-design.md` (the Phase 5 Azure section + cadences).
**Builds on:** Phases 1–4 on `main` (engine, data, db, charts, pipeline, notify, dashboard).

---

## Conventions

- New deps go in an **optional** extra so local SQLite dev, CI, and the existing tests stay lean and offline: `pyproject.toml` → `[project.optional-dependencies] azure = ["pyodbc>=5.1", "azure-identity>=1.17", "azure-keyvault-secrets>=4.8", "azure-storage-blob>=12.20", "alembic>=1.13"]`. **Do not** add these to base `dependencies`. The container installs `.[azure]`; CI keeps running `pip install -e ".[dev]"`. So Alembic itself is NOT importable in CI — the offline Alembic test (Task 6) must skip cleanly when `alembic` is absent (`pytest.importorskip("alembic")`), and CI does not type-check/lint `alembic/` (see below).
- **Azure SDKs (and `alembic`) are imported lazily, inside the functions that need them** (never at module top of `charts`/`pdf`/`dashboard`/`session`/`pipeline`), so pure-local runs and CI never import an Azure/Alembic package.
- **Backend selection is env-var-driven, mirroring `SWING_DB_URL`:** blob backend active iff `SWING_BLOB_ACCOUNT_URL` is set; Key Vault fallback active iff `KEY_VAULT_URL` is set; the mssql path active iff the DB URL starts with `mssql`. Unset everything → today's exact local SQLite behavior, so all existing tests are untouched.
- **Tests never touch Azure or the network.** Monkeypatch the new seams (the blob `upload`/`download`, the secrets resolver, the mssql engine kwargs, the exit-check fetch) exactly as existing tests monkeypatch `fetch._download` / `quotes.latest_closes` / `smtplib`.
- **Lint/type coverage of new code:** files under `src/swing_screener/` (settings, config_secrets, storage, ops, the exit-check entrypoint) ARE covered by the existing `ruff check src tests` + `mypy` (packages=["swing_screener"]) gate. `alembic/env.py` and `alembic/versions/*` are **NOT** under that package, and `infra/*` (Bicep) and `.github/workflows/*` are not Python. To keep the gate honest, **extend `ruff check` to also lint `alembic/`** (add `alembic` to the `ruff check` args in `ci.yml`; mypy can ignore generated migration bodies). Bicep is validated by `az bicep build` (Tasks 11–12) and the CD workflow by a `act`/dry-run note, not by ruff/mypy. Do not claim "the full gate validates this task" for the infra/migration files — verify them with their listed commands instead.
- Each CODE task: failing test → run (fail) → minimal impl → run (pass) → full gate (`ruff check src tests alembic`, `mypy`, `pytest -q`) → commit. Each INFRA task: write the file(s) → run the listed verify command(s) → commit. Branch `phase5-azure`; small independent commits per task; PR into `main` at the end (CI gates merge).
- Infra resource names are globally unique — derive from a `resourceToken = uniqueString(...)` in Bicep; never hardcode. **No secret values, keys, passwords, or connection strings are ever committed** (Key Vault values are `@secure()` params; auth is managed-identity/Entra throughout — there are no SQL logins, storage keys, or service-principal secrets to store).

---

## Task 1: Bound string columns + EmailLog uniqueness (unblock Azure SQL indexing + concurrency-safe idempotency)

**Files:** `src/swing_screener/db/models.py`; Test `tests/db/test_models_schema.py`.

Two schema-correctness fixes that must land before any mssql/Alembic work, because they are hard blockers there but harmless on SQLite:

1. **Length every unbounded `Mapped[str]`.** On mssql a length-less `Mapped[str]` maps to `NVARCHAR(max)`, and Azure SQL **cannot create an index on `NVARCHAR(max)`** — the initial migration's `CREATE INDEX` on `signals.ticker`, `trades.ticker`, `paper_trades.ticker` (all `index=True`) would fail. Give explicit `String(n)` to every string column, e.g. `ticker = mapped_column(String(16), index=True)`; `timeframe`/`horizon`/`status`/`fill_status`/`exit_reason`/`tier`/`reason`/`quality_tier`/`volatility_tier`/`exchange`/`kind` → `String(32)`; `name`/`subject`/`message`/`notes` → `String(256)`; `chart_path` → `String(512)` (blob keys are short, but allow headroom). Keep the same `index=`/`default=`/nullability. `Universe.ticker` (PK) also needs a bounded `String(16)` (mssql PK cannot be `NVARCHAR(max)`).
2. **Add a uniqueness guarantee for idempotency.** `EmailLog` idempotency is currently a check-then-insert (`_already_sent`), safe only single-threaded. Two overlapping Azure SQL job replicas could double-send. Add a table-level `UniqueConstraint("kind", "run_date", "alert_key", name="uq_email_log_dedup")` (the new `alert_key` column comes from Task 4) so the DB enforces dedup atomically; the orchestrator catches `IntegrityError` on insert and treats it as "already sent". Confirm `int` PKs still emit as IDENTITY on mssql (eyeball the Task 6 DDL).

**Tests** (SQLite, pure): assert the mapped columns now carry a bounded `String` length (`Signal.__table__.c.ticker.type.length == 16`, etc.) for every indexed ticker column and a representative sample; assert the `EmailLog` table has the named unique constraint over `(kind, run_date, alert_key)`. No mssql needed — this is metadata assertion. Existing tests must stay green (lengths don't change SQLite behavior). Commit: `feat: bound string columns + email_log unique constraint`.

## Task 2: `get_engine` mssql support (passwordless, pool_pre_ping, Alembic-owned schema)

**Files:** `src/swing_screener/db/session.py`; `pyproject.toml` (add the `azure` extra); Test `tests/db/test_session_mssql.py`.

Extend `get_engine(url)` keeping the existing two behaviors verbatim (the `:memory:` `StaticPool`/`check_same_thread` branch the tests rely on, and the plain-sqlite branch that calls `Base.metadata.create_all`). Add an `elif url.startswith("mssql"):` branch that:
- builds the engine with `pool_pre_ping=True` and `pool_recycle=3600` (survives serverless auto-pause/resume and idle drops),
- does **NOT** call `Base.metadata.create_all` (Alembic owns the Azure SQL schema; `create_all` cannot ALTER existing tables),
- relies on the URL carrying `Authentication=ActiveDirectoryMSI` + `User Id=<UAMI clientId>` (jobs) or `Authentication=ActiveDirectoryDefault` (local dashboard) so the ODBC driver acquires/refreshes the Entra token itself — **no** `azure-identity`/`struct.pack`/`do_connect` listener required. **Critical (runtime-only failure modes), document in a comment:** the `User Id` value MUST be the UAMI **client id** (application id), not its principal/object id; and the literal space in `User Id=` inside an env-var-sourced ODBC string must survive parsing — keep the value as one ODBC keyword inside the URL's `odbc_connect`/query and verify against a live connection in Task 12. Document `attrs_before={1256: token_struct}` only as a fallback.

**Tests** (no DB driver needed — monkeypatch `create_engine` to a recorder): an `mssql+pyodbc://...` URL builds an engine with `pool_pre_ping=True` and does **not** call `create_all`; re-assert a `:memory:` URL still gets `StaticPool` + `create_all` and a file-sqlite URL still gets `create_all`. No real ODBC connection. Commit: `feat: get_engine mssql (passwordless, no create_all)`.

## Task 3: Real-trade exit-check entrypoint (the intraday job's missing engine)

**Files:** `src/swing_screener/pipeline/exitcheck.py` (new), `src/swing_screener/notify/run.py` (add `exit` kind dispatch); Test `tests/pipeline/test_exitcheck.py`.

**Why this task exists (blocking gap in the draft):** the scheduled intraday-exit job ran `notify.run --kind exit`, but (a) `notify/run.py` argparse only accepts `{daily,weekly,monthly}`, so `--kind exit` crashes, and (b) **no code anywhere writes exit events for real trades** — `record_exit_event` is called only from `pipeline/shadow.py` with `is_paper=True` (paper book), while `pending_exit_alerts` reads `is_paper=False`. So the hourly job would error or be a permanent no-op. This task supplies the missing producer.

`run_exit_check(*, db_url, today=None, latest_bars_fn=quotes.latest_intraday_bars) -> ExitCheckResult(n_open, n_exited)`:
- Load open **real** trades via `repo.get_open_trades(session)`.
- Fetch the latest bar per open ticker through an injectable seam (`latest_bars_fn`) returning the `{low, high, close, shaved_head, ...}` mapping `evaluate_exit` already consumes. Reuse `signals/exits.evaluate_exit(OpenTrade(...), bar, cfg)` — the exact logic the shadow book uses, so paper and real exits agree. (For the bar source, reuse the `dashboard/quotes` fetch pattern over `data.fetch`; add `latest_intraday_bars` there or build the mapping inline. Keep it an injectable seam so tests stay offline.)
- For each open trade whose decision is `EXIT`, call `repo.record_exit_event(session, is_paper=False, trade_id=t.id, tier=decision.tier or "", reason=decision.reason or "", message=f"{t.ticker} {t.timeframe} {decision.reason}", created_date=today)`. **Do NOT auto-close the real `Trade` row** — the human closes trades in the dashboard; this only *alerts*. Make it idempotent within the day: skip writing an `ExitEvent` for a `(trade_id, reason, created_date)` that already exists, so re-running the hourly job doesn't pile up duplicates.

Then in `notify/run.py`: add `"exit"` to the `--kind` choices and dispatch it to a thin path that (1) runs `run_exit_check` to *produce* today's real exit events, then (2) sends the exit-alert email via the existing `pending_exit_alerts` → `compose_exit_alert` flow (reuse `send_digest`'s alert block, or factor it into `send_exit_alert(...)`). The intraday job calls `notify.run --kind exit`.

**Tests** (temp SQLite, injected `latest_bars_fn`): seed two open real trades — one whose bar trips the hard stop, one that holds; run `run_exit_check` → assert exactly one `is_paper=False` `ExitEvent` with `reason="stop"` is written and the holder writes none; run again → assert no duplicate event (idempotent within the day); assert the real `Trade` rows are NOT mutated. Then a `notify.run --kind exit` test with injected fakes asserts the produced event flows into one exit-alert email. No network. Commit: `feat: real-trade exit-check entrypoint + notify --kind exit`.

## Task 4: Per-event exit-alert idempotency (so the hourly cadence can send more than one alert/day)

**Files:** `src/swing_screener/db/models.py` (add `EmailLog.alert_key`), `src/swing_screener/notify/run.py`; Test extend `tests/notify/test_run.py`.

**Why (blocking gap):** today the exit alert is keyed by `EmailLog(kind="exit", run_date)` (`_already_sent`), so **at most one exit email goes out per calendar day** no matter how many new exits fire later in the session — which defeats the entire point of an hourly intraday cadence. Make the exit-alert idempotency *per exit-event-set* instead of per-day:
- Add `alert_key: Mapped[str] = mapped_column(String(64), default="")` to `EmailLog` (digests use `""`; the Task 1 unique constraint is over `(kind, run_date, alert_key)`).
- For the exit kind, compute a deterministic key over the **set of exit-event ids** included in that alert (e.g. a short hash of the sorted `ExitEvent.id`s, or `max(id)`), and gate the send on whether an `EmailLog(kind="exit", run_date, alert_key=<that key>)` already exists. A later hour with a *new* exit produces a *different* key → a second alert is sent; a re-run within the same hour with the *same* events is a no-op. Digest kinds keep the existing once-per-`(kind, run_date)` behavior (their `alert_key=""`).
- Wrap the `EmailLog` insert so a concurrent replica that wins the race triggers `IntegrityError` on the unique constraint → caught and treated as "already sent" (no double email).

**Tests:** fire an alert for event-set {1,2} → sent + logged; re-run same set → no-op; add event 3 and re-run → a *second* alert sent (proves the hourly cadence works); simulate the unique-constraint `IntegrityError` on insert → no exception escapes and no duplicate send. Commit: `feat: per-event exit-alert idempotency`.

## Task 5: Settings module + secrets resolver + dashboard wiring (absolute paths, env-first config, no stored creds)

**Files:** `src/swing_screener/settings.py`, `src/swing_screener/config_secrets.py`; refactor `src/swing_screener/notify/smtp.py`, `src/swing_screener/notify/run.py`, `src/swing_screener/notify/analysis.py`, **and** `src/swing_screener/dashboard/app.py`; Tests `tests/test_settings.py`, `tests/test_config_secrets.py`.

**settings.py** — a tiny pydantic-free module that reads env once and resolves all paths to **absolute** (relative defaults are wrong in a container): `SWING_DB_URL` (default `sqlite:///local.db`), `SWING_CHART_DIR`/`SWING_CACHE_DIR`/`SWING_PDF_DIR` (defaults the repo-relative `.charts`/`.cache`/`.digests` but `Path(...).resolve()`d), `SWING_BLOB_ACCOUNT_URL`, `SWING_BLOB_CONTAINER`, plus pass-throughs for `ANTHROPIC_API_KEY`/`GMAIL_*`/`DIGEST_TO`/`KEY_VAULT_URL`/`AZURE_CLIENT_ID`. Both CLIs **and the dashboard** import defaults from here. Specifically replace `dashboard/app.py`'s module-level `DB_URL = os.environ.get("SWING_DB_URL", ...)` (line 28) and `_cache_dir()` (lines 40–41) with the settings values (absolute), so the local dashboard pointed at Azure SQL reads the same config the jobs do.

**config_secrets.py** — `get_secret(name, *, default=None) -> str | None` with precedence: (1) `os.environ[name]`; (2) if `KEY_VAULT_URL` set, fetch from Key Vault via `SecretClient(vault_url, DefaultAzureCredential())` mapping `ANTHROPIC_API_KEY → anthropic-api-key` (underscore→hyphen, lowercase), cached, **azure imports lazy inside the function**, failure logs only the secret **NAME, never the value** and falls through; (3) `default`. Plus `require_secret(name)` raising a clear `RuntimeError` if unresolved. In Azure the Container Apps Key Vault reference injects these as plain env vars, so path (1) wins with zero behavior change — this is a centralization/forward-compat seam. Refactor the three notify call sites through it, keeping the existing arg-override seams so current tests pass (env still wins): `smtp.py` `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` → `require_secret`; `notify/run.py` `DIGEST_TO` → `get_secret` (keep its fail-fast guard); `analysis.py` `anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))` (its try/except already degrades). **Confirm no secret VALUE is ever logged** on any failure path in `analysis.py`/`smtp.py`/`config_secrets.py` (name-only).

**Tests:** env unset → the three dirs resolve to absolute paths equal to the old repo-relative defaults resolved; `SWING_CHART_DIR=/data/charts` → absolute + exact; `SWING_DB_URL` default `sqlite:///local.db`; secrets: env-present returns the env value with **no** azure import; `KEY_VAULT_URL` unset returns the default; underscore→hyphen mapping correct; never imports azure; never logs a value. Monkeypatch env only. Commit: `feat: settings module + env-first/key-vault secrets resolver + dashboard wiring`.

## Task 6: Blob chart store (pluggable, private, graceful, exact-key) + consumers

**Files:** `src/swing_screener/storage/__init__.py`, `src/swing_screener/storage/blob.py`; wire `src/swing_screener/pipeline/run.py`, `src/swing_screener/notify/pdf.py`, `src/swing_screener/dashboard/app.py`; Test `tests/test_storage_blob.py`.

`blob.py` exposes `blob_enabled() -> bool` (true iff `SWING_BLOB_ACCOUNT_URL` set), `upload_chart(local_path: Path, key: str) -> str`, and `download_bytes(key: str) -> bytes`, building `BlobServiceClient(SWING_BLOB_ACCOUNT_URL, credential=DefaultAzureCredential()).get_container_client(SWING_BLOB_CONTAINER)` lazily/once (`upload_blob(name=key, data=..., overwrite=True)`; `get_blob_client(key).download_blob().readall()`). **azure-storage-blob is imported inside this module only.** `render_chart` is unchanged — mplfinance must `savefig` to a real local file; **the pipeline owns the upload**.

Wire producer/consumers, each branched on `blob_enabled()` and preserving today's exact local-path behavior when disabled. **The blob KEY must exactly match the persisted filename scheme so the consumers read the same string back:**
- `pipeline/run.py` (the chart loop at lines 111–116; `chart_path` is set at **line 114**): the local file is `f"{r.ticker}_{r.timeframe}_{today:%Y%m%d}.png"`. Use that **same basename** as the blob key under a date prefix: `key = f"{today:%Y%m%d}/{r.ticker}_{r.timeframe}_{today:%Y%m%d}.png"`. After `render_chart` writes the local PNG, if blob enabled call `upload_chart(local_path, key)` and set `signals[rank-1].chart_path = key`; else store `str(path)` as today. (The key string stored on `Signal.chart_path` is the literal value read back by pdf/dashboard — they must be byte-identical.)
- `notify/pdf.py` (line 73): today this guards on `p.chart_path and Path(p.chart_path).exists()`. On the blob branch a key is **not** a filesystem path, so `Path(key).exists()` is `False` and the chart would silently vanish — **the `.exists()` guard must be removed/replaced on the blob branch.** If `blob_enabled()`: `Image(io.BytesIO(download_bytes(p.chart_path)), width=..., height=...)` (reportlab accepts a file-like — pass `BytesIO`, not raw bytes), wrapped in try/except so a missing/aged-out blob degrades to a chartless section exactly like today. Else: the existing `Path(...).exists()` local branch.
- `dashboard/app.py` (line 72, same `.exists()` issue): if `blob_enabled()`: `st.image(download_bytes(s.chart_path), caption=s.ticker)` in try/except (skip the image on failure); else the existing local-path `.exists()` branch.

**Tests:** monkeypatch `storage.blob.upload_chart`/`download_bytes` (network-free, mirroring the fetch seam). Assert the pipeline stores the **exact key** `f"{today:%Y%m%d}/{ticker}_{tf}_{today:%Y%m%d}.png"` (not a filesystem path) when blob is enabled and calls `upload_chart`; assert pdf/dashboard call `download_bytes(key)` and do **not** stat the filesystem on the blob branch; assert a `download_bytes` that raises degrades to no-image rather than crashing; assert blob-disabled keeps the local path + the `.exists()` behavior. No Azure. Commit: `feat: pluggable private blob chart store (exact-key, graceful)`.

## Task 7: Alembic migrations (Alembic owns the Azure SQL schema)

**Files:** `alembic.ini`, `alembic/env.py`, `alembic/versions/<rev>_initial.py`; Test `tests/test_alembic_offline.py`.

`alembic init alembic`; in `env.py` set `target_metadata = swing_screener.db.models.Base.metadata` and read the URL from `SWING_DB_URL` via the settings module. **Autogenerate the initial revision against a real mssql dialect target**, not sqlite (mssql maps Boolean→BIT and has different IDENTITY/PK semantics). Because no Azure SQL server exists when this task runs (Bicep is Task 11), use one of, in order of preference:
1. a **local SQL Server container** for autogen only — `docker run -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD=<dev-only-throwaway> -p 1433:1433 mcr.microsoft.com/mssql/server:2022-latest` — point `SWING_DB_URL` at it (`mssql+pyodbc://sa:<pw>@localhost:1433/master?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no&TrustServerCertificate=yes`), autogenerate, then **discard the container** (this SA password is a local-only ephemeral, never committed, never used in Azure — Azure is Entra-only); or
2. if Docker/ODBC is unavailable on the dev box, **generate offline and hand-fix the dialect mapping** (Boolean→BIT, bounded `NVARCHAR(n)` from Task 1, IDENTITY on int PKs) by eyeballing the emitted DDL.

The migration must cover all six existing tables (`universe`, `signals`, `trades`, `paper_trades`, `exit_events`, `email_log`) **including** the Task 1 `String(n)` lengths, the three `ticker` indexes (now indexable because they are bounded), and the `email_log` `UniqueConstraint`. The pipeline (Task 8) runs `alembic upgrade head` before the screen.

**Tests** (offline, no live DB; `pytest.importorskip("alembic")` so CI without the `azure` extra skips): run Alembic in offline/SQL-emit mode (`alembic upgrade head --sql`) or assert `command.upgrade` is invoked; assert the six table names appear in the emitted SQL, that no `NVARCHAR(max)` appears on an indexed column, and that `env.py` resolves `target_metadata` to `Base.metadata`. Commit: `feat: alembic migrations for the six tables (mssql-correct)`.

## Task 8: Run migrations on startup + fail-fast on missing cloud DB (resilient to cold resume)

**Files:** `src/swing_screener/pipeline/run.py`; Test extend `tests/pipeline/test_run.py` (or new `tests/pipeline/test_run_mssql.py`).

At the top of the run (after parsing `--db`, before fetch), when the URL starts with `mssql`, invoke `alembic upgrade head` (via `alembic.command.upgrade` with a config pointing at the URL, **lazy import**) wrapped in a **40613-tolerant retry** (3–5 attempts, ~10–30s backoff) so the first request of the day survives serverless auto-resume (~1 min). **Fail-fast** if running in a container without a real DB: if `SWING_DB_URL` is unset the CLI must not silently create a throwaway SQLite file in the container — require the mssql URL when an Azure marker (`KEY_VAULT_URL` or an explicit `SWING_REQUIRE_DB`) is present. Switch the argparse defaults for `--db`/`--cache-dir`/`--chart-dir` to the settings module (absolute). Leave the sqlite path calling `get_engine`/`create_all` exactly as-is.

**Tests:** monkeypatch the alembic-upgrade seam + a fake that raises a simulated 40613 once then succeeds → assert the retry runs migrations and proceeds; assert an mssql URL does **not** call `create_all`; assert the sqlite path is unchanged; assert fail-fast raises when the Azure marker is set but `SWING_DB_URL` is missing. No DB/network. Commit: `feat: self-migrate on mssql startup with cold-resume retry`.

## Task 9: UTC timestamps + absolute paths in notify

**Files:** `src/swing_screener/notify/run.py`; Test extend `tests/notify/test_run.py`.

Replace the two naive `datetime.now()` writes for `EmailLog.sent_at` (lines 82, 125) with `datetime.now(UTC)` (`from datetime import UTC`) so stored timestamps are tz-aware UTC in a container and consistent with Azure SQL. Switch `notify/run.py` argparse defaults for `--db`/`--pdf-dir` to the settings module (absolute). No schema change; `EmailLog.sent_at` stays `Mapped[datetime]` but stores UTC.

**Tests:** seed a temp DB, run a digest with injected fakes (as the existing test), assert the recorded `sent_at` is tz-aware and UTC; idempotency still holds. Commit: `fix: UTC timestamps + absolute paths in notify`.

## Task 10: Dockerfile + .dockerignore + Eastern-gate entrypoint (DST + monthly gating, digest-pinned base, local smoke test)

**Files:** `Dockerfile` (repo root), `.dockerignore` (repo root), `src/swing_screener/ops/__init__.py`, `src/swing_screener/ops/eastern_gate.py`; Test `tests/ops/test_eastern_gate.py`.

`eastern_gate.py` is the container **ENTRYPOINT** wrapper: it reads `RUN_IF_ET_HOUR` (e.g. `"16"`, `"8"`, or a comma list `"9,10,11,12,13,14,15"`), compares to `datetime.now(ZoneInfo("America/New_York")).hour`, and either `os.execvp`s the passed CLI command+args or **exits 0** immediately. It also supports `RUN_IF_LAST_BUSINESS_DAY=1` for the monthly job (last business day of the current month, computed in `America/New_York`). Neither DST nor "last session of month" is expressible in 5-field UTC cron, so both reduce to this Python gate.

`Dockerfile`: base `python:3.12-slim` **pinned by digest** (`python:3.12-slim@sha256:<digest>`) so the build is reproducible and not silently re-pulled; ONE apt layer installs `curl gnupg2 apt-transport-https ca-certificates`, `unixodbc unixodbc-dev`, and the matplotlib/reportlab native libs `libfreetype6 libpng16-16 fontconfig`; add Microsoft's Debian 12 repo and `ACCEPT_EULA=Y apt-get install -y msodbcsql18`. **Comment the known fragility:** Microsoft periodically rotates the repo signing key / URL, which breaks this layer; pin the `packages.microsoft.com` keyring fetch and document that if CD's `docker build` fails on this layer, the fix is to refresh the key/repo (see Task 14 for the CD build-cache/retry note). Then copy `pyproject.toml` + `src/` + `alembic/` + `alembic.ini`; `pip install --no-cache-dir ".[azure]"`; set a non-root `USER`; `WORKDIR /app`; `ENTRYPOINT ["python","-m","swing_screener.ops.eastern_gate"]` + a default `CMD`. `.dockerignore` excludes `.venv .git .mypy_cache .pytest_cache .ruff_cache .cache .charts .digests *.db local.db tests/ **/__pycache__` so local SQLite/charts/cache never bake into the image.

**Tests** (the gate is unit-testable; the image is not): monkeypatch the clock so `RUN_IF_ET_HOUR="16"` at 16:xx ET → gate `exec`s the command (assert via a recorded fake exec); at 09:xx ET → exits 0 without exec; comma-list membership works; `RUN_IF_LAST_BUSINESS_DAY` gates correctly on a known last-business-day vs not. **Verify the image manually** (not in CI; ODBC + native libs fail at runtime, not build): `docker build -t swing-screener:dev .`, then:
- `docker run --rm -e RUN_IF_ET_HOUR=$(TZ=America/New_York date +%H) swing-screener:dev -m swing_screener.pipeline.run --help`
- exercise a chart/PDF render path against sqlite to confirm `libfreetype6`/`libpng16-16`/`fontconfig` are present.

Commit: `feat: Dockerfile (digest-pinned) + eastern-gate entrypoint`.

## Task 11: Bicep IaC — registry, identity, env, storage, key vault, SQL (Basic-DTU default, tight firewall, capped logs)

**Files:** `infra/main.bicep` (`targetScope='subscription'`, creates the RG), `infra/modules/acr.bicep`, `infra/modules/identity.bicep`, `infra/modules/env.bicep`, `infra/modules/storage.bicep`, `infra/modules/keyvault.bicep`, `infra/modules/sql.bicep`, `infra/main.bicepparam`.

Parameterize `location`, env name, and `resourceToken = uniqueString(subscription().id, rgName)` (globally-unique ACR/storage/KV/SQL names; respect limits — ACR alphanumeric 5–50, storage 3–24 lowercase alnum). Modules:
- **identity.bicep** — ONE `userAssignedIdentity`; export `resourceId`/`principalId`/`clientId` for the others. (This single UAMI is the only auth principal: ACR pull, Blob, Key Vault, and SQL all authorize it — there are no stored credentials anywhere.)
- **acr.bicep** — `registries` SKU `Basic`, `adminUserEnabled=false`; role assignment granting the UAMI **AcrPull** scoped to the registry. **Re-verify** the AcrPull role-definition GUID via `az role definition list --name AcrPull --query "[0].name"` before hardcoding (commonly `7f951dda-4ed3-4680-a7ca-43fe172d538d`).
- **env.bicep** — Log Analytics workspace + `Microsoft.App/managedEnvironments` (Consumption) wired to it. **Bound ingestion cost:** set the workspace `dailyQuotaGb` (e.g. 0.5) and `retentionInDays` (e.g. 30) — the screen logs every failed ticker at WARNING across ~1–2k tickers and the intraday job runs ~9×/day, so an uncapped workspace is not "pennies".
- **storage.bicep** — `StorageV2` `Standard_LRS`, `allowBlobPublicAccess=false`; one private container `charts` (`publicAccess:'None'`); UAMI **Storage Blob Data Contributor** scoped to the account (re-verify GUID, commonly `ba92f5b4-2d11-453d-a403-e96b0029c9fe`); a lifecycle rule deleting chart blobs older than ~90 days. **Comment:** `Signal.chart_path` rows persist past the 90-day blob TTL, so historical signals lose their charts; this degrades gracefully (Task 6 try/except → chartless) and is documented in the runbook.
- **keyvault.bicep** — Vault `enableRbacAuthorization=true`; child secrets `anthropic-api-key`/`gmail-address`/`gmail-app-password`/`digest-to` from `@secure()` params (never committed); UAMI **Key Vault Secrets User** scoped to the vault (re-verify GUID, commonly `4633458b-17de-408a-b874-0445c86b69e6`).
- **sql.bicep** — `Microsoft.Sql/servers` with `azureADOnlyAuthentication=true` and your Entra user/group as AAD admin (**no SQL login/password anywhere**). **Default the database to Basic DTU** (`sku {name:'Basic', tier:'Basic'}`, ~$5/mo flat, no cold start) — this matches the recommended cost path and avoids cold-start latency, and the hourly intraday job keeps the DB warm through market hours so serverless would rarely auto-pause and likely cost *more*. **Comment + a single `useServerless` param toggle** for the GP serverless alternative (`sku {name:'GP_S_Gen5',tier:'GeneralPurpose',family:'Gen5',capacity:2}`, `minCapacity:0.5`, `autoPauseDelay:60`) — one-line opt-in only if idle time genuinely dominates. **Firewall (security):** do **not** use the broad `AllowAllWindowsAzureIps` (0.0.0.0) rule — it opens the server to every Azure tenant's outbound IPs. Restrict to the Container Apps environment's static outbound IP(s) (read from the env resource) plus your home IP for the local dashboard; note a private endpoint as the stronger Phase-6 option. Entra-only auth still gates access, but minimize the network surface.

**Verify:** `az bicep build --file infra/main.bicep` compiles clean; `az deployment sub create --location <loc> --template-file infra/main.bicep --parameters infra/main.bicepparam --what-if` shows the expected resource set with no errors (do **not** apply yet). Commit: `feat: bicep IaC (Basic-DTU SQL, tight firewall, capped logs)`.

## Task 12: Bicep IaC — the five scheduled Jobs (UTC cron + Python ET gate)

**Files:** `infra/modules/jobs.bicep`; reference from `infra/main.bicep`.

Five `Microsoft.App/jobs`, all sharing the image and the one UAMI (`identity.type:'UserAssigned'`), `configuration.triggerType:'Schedule'`, `replicaTimeout` (3600 screen, ~1800 digests), `replicaRetryLimit:1`, `replicaCompletionCount:1`, `registries[]` `{server:<acr>.azurecr.io, identity:<UAMI id>}`, `secrets[]` each `{name, keyVaultUrl, identity:<UAMI id>}`, `template.containers[0]` `image=<acr>/swing-screener:<tag>` with env mapping `secretref:`→the env vars the code already reads (`ANTHROPIC_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `DIGEST_TO`) plus `SWING_DB_URL` (mssql, `Authentication=ActiveDirectoryMSI` + `User Id=<UAMI clientId>`), `SWING_BLOB_ACCOUNT_URL`/`SWING_BLOB_CONTAINER`, `SWING_CHART_DIR=/data/charts`, `KEY_VAULT_URL`, and **`AZURE_CLIENT_ID=<UAMI clientId>`** (required so `DefaultAzureCredential` selects the right user-assigned identity for Blob/KV — and the same client id is the SQL `User Id`). Each job differs only by **cron + command/args + the gate** — UTC crons fire on BOTH EST (UTC−5) and EDT (UTC−4); the Python gate picks the correct ET hour and prevents wrong-hour fires:

| Job | cron (UTC) | command | gate |
|---|---|---|---|
| evening-screen (~16:15 ET) | `15 20,21 * * 1-5` | `python -m swing_screener.pipeline.run` | `RUN_IF_ET_HOUR=16` |
| daily-digest (~08:00 ET pre-open) | `0 12,13 * * 1-5` | `... notify.run --kind daily` | `RUN_IF_ET_HOUR=8` |
| intraday-exit (session hrs) | `0 13-21 * * 1-5` | `... notify.run --kind exit` (Task 3) | `RUN_IF_ET_HOUR=9,10,11,12,13,14,15,16` |
| weekly-digest (after Fri close) | `30 20,21 * * 5` | `... notify.run --kind weekly` | `RUN_IF_ET_HOUR=16` |
| monthly-digest (last session) | `30 20,21 * * 1-5` | `... notify.run --kind monthly` | `RUN_IF_ET_HOUR=16` + `RUN_IF_LAST_BUSINESS_DAY=1` |

**DST double-fire safety (must hold):** the gate compares `.hour` equality, so on the spring-forward/fall-back day a UTC cron pair can fire zero or two times for the intended ET hour. This is safe **only because the workloads are idempotent**: the screen does delete+reinsert per `run_date` (`delete_signals_for` / `delete_paper_trades_opened_on`) and `advance_open` guards on `last_advanced == today` (won't double-advance); the digests/exit alerts are idempotent via `EmailLog` (Tasks 1/4). A second same-day fire therefore re-screens to the same rows and re-sends nothing. Task 17 adds an explicit test proving the screen is safe under a double-fire.

**Friday month-end overlap (must hold):** weekly-digest (`* * 5`) and monthly-digest (`* * 1-5`, gated by last-business-day) both fire `30 20,21` and on a month-ending Friday run at the same minute; each independently attempts the exit-alert send + `EmailLog` insert. The `(kind, run_date, alert_key)` unique constraint (Tasks 1/4) makes the concurrent exit-alert insert atomic (the loser catches `IntegrityError`), so no double exit email; the two *digests* have different `kind`s so they don't collide. Note this in a comment.

**Verify:** `az bicep build` compiles; `--what-if` shows five jobs with the expected cron/args/gates. Commit: `feat: bicep five scheduled container-apps jobs`.

## Task 13: yfinance-from-Azure mitigation + go/no-go gate (do not defer — existential risk)

**Files:** `src/swing_screener/data/fetch.py` (optional retry/backoff knob), `docs/azure-deploy.md` (the go/no-go section); Test extend `tests/data/test_fetch.py` if a backoff knob is added.

**Why (existential risk, not deferrable):** the screen fetches ~1–2k tickers × multiple intervals; **from Azure datacenter IPs yfinance is heavily rate-limited/blocked**, which can yield near-100% failures and a cloud screen that produces *zero signals*. The home-IP local dry-run will NOT surface this — it only appears post-deploy. Mitigations, in order:
1. Add a small **retry-with-jitter/backoff** around the per-ticker fetch (the run already isolates failures and counts `n_failed`), and consider chunking/throttling requests to look less bot-like.
2. Make Task 14's first real cloud screen an explicit **go/no-go gate**: run `evening-screen` once, read `n_failed` vs total from the logs; if the failure rate is materially high (e.g. >25%), **do not** proceed to wire the cadence — instead apply a fallback before going live: route yfinance through an outbound proxy/NAT with a non-Azure egress IP, or swap the data source (e.g. a keyed provider) behind the existing `data.fetch` seam.
3. Document a `n_failed` threshold alert as a fast-follow if rate-limiting proves intermittent.

This is a real Task with a hard decision gate — the deployment is not "done" until the cloud screen demonstrably produces signals. **Tests** (if a backoff knob is added): assert the retry/backoff is invoked on a simulated transient failure and gives up after N attempts; the seam stays offline-mockable. Commit: `feat: yfinance backoff + Azure-IP go/no-go gate`.

## Task 14: First deploy + one-time SQL grants (cutover dry-run, explicit command checklist)

**Files:** `infra/post-deploy.sql`, `docs/azure-deploy.md` (runbook).

Manual cutover into a **throwaway resource group**, executed once, recorded in the runbook. **State plainly up front: this is a clean cutover — historical rows in the local `local.db` (signals, trades, paper trades) are NOT migrated and are abandoned; the Azure SQL store starts empty and Alembic builds the schema on first run.** Steps:
1. `az deployment sub create ... --parameters <secrets via @secure()>` to provision everything (Tasks 11–12) — secrets passed at deploy time only, never committed.
2. Build+push the image: `az acr build --registry <acr> --image swing-screener:bootstrap .` (server-side build is more reliable for the msodbcsql18 layer than a local/runner `docker build`), or local `docker build`+`push` after `az acr login`.
3. **One-time T-SQL** (`post-deploy.sql`, run as the Entra admin via `sqlcmd`/Query editor — Bicep cannot create contained users). Explicit checklist:
   - `CREATE USER [<uami-name>] FROM EXTERNAL PROVIDER;`
   - `ALTER ROLE db_datareader ADD MEMBER [<uami-name>];`
   - `ALTER ROLE db_datawriter ADD MEMBER [<uami-name>];`
   - `ALTER ROLE db_ddladmin ADD MEMBER [<uami-name>];` (Alembic issues CREATE/ALTER TABLE)
   - Repeat `CREATE USER [<your-entra-upn>] FROM EXTERNAL PROVIDER;` + `db_datareader` for **your own** Entra user so the local dashboard can read.
   - Wait a few minutes for Entra propagation.
4. **Local-dashboard role grants (explicit, not prose):** assign your human Entra identity **Storage Blob Data Reader** on the storage account: `az role assignment create --assignee <your-object-id> --role "Storage Blob Data Reader" --scope <storage-account-resource-id>` (re-verify the role name/GUID). Combined with the SQL `db_datareader` above, the local dashboard reads candidates + downloads charts.
5. **First cloud screen = the Task 13 go/no-go gate.** Manual run (don't wait for cron): `az containerapp job start --name evening-screen -g <rg>` (override `RUN_IF_ET_HOUR` to the current ET hour so the gate passes). Verify `alembic upgrade head` created the six tables, signals persisted to Azure SQL, chart PNGs landed in the private `charts` container, **and read `n_failed`** — if the Azure-IP failure rate is too high, stop and apply the Task 13 fallback before continuing. Then run `daily-digest` and confirm a digest email; run `intraday-exit` against a seeded open real trade and confirm a real `ExitEvent` + alert.
6. Point the local dashboard at Azure SQL: `az login`, set `SWING_DB_URL=mssql+...&Authentication=ActiveDirectoryDefault`, `SWING_BLOB_ACCOUNT_URL`/`CONTAINER`, confirm it reads candidates + downloads charts.

**Verify:** `az containerapp job execution list --name <job> -o table` shows `Succeeded`; logs via the Log Analytics `ContainerAppConsoleLogs_CL` query filtered by execution name. Commit: `docs: azure deploy runbook + post-deploy SQL grants`.

## Task 15: CD — GitHub Actions → Azure via OIDC (CI-gated, verified roles, lands last)

**Files:** `.github/workflows/cd.yml`; `docs/azure-deploy.md` (CD section).

One new workflow, `on: push: branches: [main]`, top-level `permissions:{ contents: read, id-token: write }`. Job `test` re-runs the exact `ruff`/`mypy`/`pytest` steps (self-gating — simpler and safer than fragile cross-workflow `workflow_run`); job `deploy: needs: test` (so **CD is CI-gated** — deploy never runs unless the same lint/type/test gate is green). Deploy steps: `actions/checkout@v4` → `azure/login@v2` (**OIDC; `client-id`/`tenant-id`/`subscription-id` from repo secrets, NO passwords or client secrets**) → `az acr login --name <acr>` → `docker build` (tag both `${{ github.sha }}` and `latest`) → `docker push --all-tags` → loop `az containerapp job update -n <job> -g <rg> --image <acr>.azurecr.io/swing-screener:${{ github.sha }}` over the five jobs. Leave `ci.yml` unchanged as the PR/branch gate. **Build reliability:** add a build retry around the msodbcsql18 layer or enable Docker layer caching, and document that if the MS repo key/URL rotates, `az acr build` (server-side) is the reliable fallback for that exact layer (Task 14 already builds via `az acr build`).

One-time setup (documented, not in the workflow):
- Create the deploy identity (app registration/SP **or** UAMI); add a **federated credential** with subject **exactly** `repo:solorzao/swing-screener:ref:refs/heads/main`, **audience `api://AzureADTokenExchange`**, and the GitHub OIDC **issuer `https://token.actions.githubusercontent.com`** — a wrong subject *or audience/issuer* fails token exchange silently (audience/issuer are the more common silent failures).
- Assign **least privilege only:** `AcrPush` on the ACR + a role that permits `containerapp job update` on the RG. **Re-verify the exact job-management role** — "Container Apps Jobs Contributor" may not be a real built-in role name; confirm via `az role definition list --query "[?contains(roleName,'Container Apps')].{n:roleName,id:name}" -o table` and use the confirmed role definition ID (or scope a custom role to `Microsoft.App/jobs/*`). Do **not** grant Contributor.
- Store `AZURE_CLIENT_ID`/`AZURE_TENANT_ID`/`AZURE_SUBSCRIPTION_ID` as repo secrets (these are identifiers, not secrets-with-credentials; OIDC mints short-lived tokens at run time).
- Pin jobs to the immutable `${{ github.sha }}` tag (rollback = re-run `job update --image ...:<old-sha>`). Note: `job update --image` does **not** start a run — the new image is used on the next scheduled (or manual `job start`) execution; runtime secrets stay in Key Vault, never in the image or workflow.

**Verify:** push a no-op commit to `main`; watch `test` then `deploy` go green; confirm `az containerapp job show -n evening-screen -g <rg>` reports the new SHA tag; `az containerapp job start` for an immediate smoke run. Commit: `feat: OIDC CD — build+push image, repoint jobs on merge to main`.

## Task 16: Screen double-fire idempotency test (DST safety, explicit)

**Files:** Test extend `tests/pipeline/test_run.py`.

The DST scheme can fire the screen twice (or zero times) for the intended ET hour on a transition day, and the screen has no `EmailLog`-style guard — its safety rests on delete+reinsert and `advance_open`'s `last_advanced == today` guard. Make that safety **explicit and tested**: run `run_screen` twice for the same `today` against one temp SQLite DB with the same injected fakes; assert the second run leaves the signals table and the shadow book in the same state as the first (no duplicate signals for the date, no double-advanced paper trades, no duplicate paper-trade opens for `opened_date == today`). This pins the property the DST gate depends on. Commit: `test: screen double-fire idempotency (DST safety)`.

## Task 17: Docs + finalize

- `docs/azure-deploy.md`: full runbook — resources; the **two connection-string forms** (job `Authentication=ActiveDirectoryMSI`+`User Id=<UAMI clientId>`, local `ActiveDirectoryDefault`) with the clientId-not-objectId warning; the `CREATE USER ... FROM EXTERNAL PROVIDER` + `db_ddladmin` grants and the human-user + Storage Blob Data Reader grants; the 40613 auto-resume retry; the DST/monthly Python gate; the yfinance-from-Azure go/no-go gate; the clean-cutover (local.db abandoned) note; the 90-day chart TTL caveat; OIDC/CD setup (subject + audience + issuer + verified roles).
- `docs/azure-sql.md` and updates to `docs/email-digests.md`/`docs/dashboard.md`: in Azure the env vars come from Key Vault via the job's managed identity (no stored creds); locally you export them (or set `KEY_VAULT_URL` + `az login`); the dashboard **stays local**.
- README roadmap → Phase 5 done; link the docs.
- Full gate green (`ruff check src tests alembic`, `mypy`, `pytest -q`); final holistic review (superpowers:requesting-code-review); push `phase5-azure`; CI green; open PR into `main`.

Commit: `docs: phase 5 azure deploy guide + README roadmap`.

---

## Testing notes

- **Unit-tested (CI, offline, no Azure):** bounded-column + unique-constraint metadata (Task 1), `get_engine` mssql branch (recorder), the **real-trade exit-check** producing `is_paper=False` events + idempotency (Task 3), the **per-event exit-alert idempotency** including a second-alert-per-day case (Task 4), the settings module + secrets resolver (env-first, name mapping, no value logged) + dashboard wiring (Task 5), the blob store wiring with **exact key** and `.exists()`-guard removal on the blob branch (Task 6), the startup-migration 40613 retry (Task 8), UTC timestamps (Task 9), the Eastern/last-business-day gate (Task 10), and the **screen double-fire idempotency** (Task 16). All new Azure/Alembic imports are lazy so CI's `pip install -e ".[dev]"` stays offline; the Alembic test uses `pytest.importorskip`.
- **Validated by the manual deploy dry-run (Task 14), NOT unit tests:** the Dockerfile (ODBC Driver 18 + native libs fail at runtime → a local `docker run` smoke of both CLIs is mandatory), the Bicep deployment, the five Jobs' cron/args/gates, Key Vault references resolving via the UAMI, blob read/write **across separate job executions** (the cross-job filesystem is NOT shared — charts MUST be in blob), Azure SQL passwordless auth + cold-resume, the `User Id=<clientId>` ODBC parsing, and the **yfinance `n_failed` go/no-go gate** (Task 13) which only appears from an Azure IP.
- **Alembic** owns the Azure SQL schema; `create_all` stays only for sqlite (tests/local). Autogenerate against a **real mssql dialect** (local SQL Server container or hand-fixed offline), never sqlite (Boolean→BIT; bounded `NVARCHAR(n)` from Task 1; IDENTITY on int PKs).
- **The dashboard stays local** throughout — no hosting, no public endpoint; it points at Azure SQL via `az login` (`ActiveDirectoryDefault`) and reads charts as Storage Blob Data Reader.

## Cost estimate

~**$10–12/month on the default Basic-DTU SQL path** (ACR Basic ~$5 flat + Azure SQL Basic DTU ~$5 flat + Container Apps Jobs ≈$0 within the free grant + Storage/Key Vault pennies + Log Analytics **capped** via `dailyQuotaGb`). The hourly intraday job keeps the DB warm through market hours, so GP serverless would rarely auto-pause and likely cost **more** (~$20–45/mo) while adding cold-start latency — hence Basic DTU is the **default**, with a one-line `useServerless` toggle (Task 11) only if the DB genuinely idles >16h/day. CD is ~$0 (OIDC + Actions minutes free at this volume).

## Security posture (summary)

- **No stored credentials anywhere:** one user-assigned managed identity authorizes ACR pull, Blob, Key Vault, and SQL (Entra-only, `azureADOnlyAuthentication=true`); CD uses OIDC (short-lived tokens, no client secret); the only local-only ephemeral is the optional dev SQL container SA password in Task 7, never committed and never used in Azure.
- **Secrets** live in Key Vault and are injected as env vars by the job; `config_secrets` and the SMTP/Anthropic paths log secret **names only, never values**.
- **Network:** SQL firewall restricted to the Container Apps env outbound IP(s) + your home IP (not the broad `AllowAllWindowsAzureIps`); Blob/Storage public access disabled, one private container. Private endpoints are the stronger Phase-6 option.
- **CD least privilege:** `AcrPush` + a verified Container-Apps job-management role only (not Contributor); federated credential locked to `repo:solorzao/swing-screener:ref:refs/heads/main` with the correct audience + issuer.

## Deferred

- **Phase 6:** host the dashboard in Azure (Container App + Entra auth) only if local `az login` becomes inconvenient — deliberately deferred (a 24/7 compute charge the design avoids).
- VNet/**private endpoints** for SQL/Storage/Key Vault (stronger than the firewall rules above), a dedicated migrate Job (vs. self-migrate on startup), Application Insights tracing, a GitHub Environment approval gate on CD, lifting the parquet bar cache to Blob, and a standing `n_failed` alert (vs. the Task 13 go/no-go gate) — over-engineering for a single private user now.

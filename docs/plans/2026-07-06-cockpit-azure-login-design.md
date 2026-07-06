# Cockpit: "Sign in to Azure" recovery button — design

2026-07-06. Validated with Oliver (scope, auth flow, and all three design sections
approved in conversation). Follow-up to the 2026-07-05 desktop-UI design; this is the
smallest useful slice of its "Azure switch" story, pulled forward because the very
first Azure-connected morning hit an expired-credential wall with no in-app recovery.

## Problem

The cockpit points at whatever `SWING_DB_URL` names. When that is Azure SQL and the
AAD credential has expired (or `az login` was never run), the DB chip reports down,
every panel shows its friendly 503 — and the only fix lives in a terminal the
double-click user never opened. The full Local ⇄ Azure switcher chip stays Phase 2;
this feature is recovery only.

## Decisions (chosen from alternatives)

- **Scope: login-recovery button only.** No source switching; the env var / `--db`
  flag remains the switch. (Rejected for now: full Phase-2 switcher; a two-preset
  minimal switcher.)
- **Auth: `az login` subprocess.** Refreshes the az CLI token cache — the same
  credential the cockpit's per-connection token fetch, the pipeline, and shell
  scripts already read via DefaultAzureCredential. Zero changes to the DB auth path.
  (Rejected for now: `InteractiveBrowserCredential` + persistent cache — the
  packaged-app answer, but it rewires `db/session.py` and does not refresh the az
  cache the rest of the toolchain uses. Revisit at Stage-2 packaging.)

## Backend: `POST /api/azure-login`

- Spawns `az login` detached, **fixed argv, `shell=False`, nothing from the request
  reaches the command line** (the request carries no parameters at all). Returns
  `{started: true}` immediately; the browser dance takes 10–60 s and must not block
  an API worker.
- **Azure-mode only:** 409 unless the configured URL's backend is `mssql+pyodbc`.
  An az login cannot fix a broken sqlite file, so the affordance must not exist in
  local mode.
- **Single-flight:** a module-level flag (engine-cache lock discipline) makes a
  concurrent second POST answer `{started: false, already_running: true}`. A daemon
  watcher thread clears the flag when the subprocess exits; the flag also
  self-expires at the 2-minute deadline so an abandoned browser tab cannot wedge it.
- **Missing CLI:** `az` not on PATH → `{started: false, error: "az-not-found"}`.
- **No success signal.** The endpoint never says "logged in". `db/session.py`
  already fetches a fresh AAD token per connection and the pool pre-pings, so the
  next `/api/health` poll flips `connected` on its own. Health is the single source
  of truth for recovery — same posture as the port-file probe.
- **Health payload grows `azure: bool`** (derived from the URL backend, not by
  parsing the credential-safe label).

## Frontend: the DB chip owns the whole surface

- `azure && !connected` → the chip's down state grows one button: **Sign in to
  Azure**. Local mode or healthy Azure → no button (quiet by default).
- Click → POST → disabled **"waiting for browser sign-in…"** state. The existing
  `usePolling` health loop is the only recovery detector: `connected: true` → chip
  green, button gone. One recovery path regardless of how the credential returned.
- Health still down after **2 minutes** → button re-arms as "try again"; the chip
  keeps showing the class-name-only down summary it shows today.
- `already_running: true` → keep the waiting state (double-launch converges).
- Panels need nothing: cohorts/heartbeats already poll independently and repopulate
  when the DB returns. No new colors; reuse the existing chip/button tokens.

## Security guard

Any webpage can fire a simple POST at `http://127.0.0.1:<port>`; harmless for the
read-only routes, but this one spawns a process. The endpoint therefore requires a
custom header (`X-Cockpit: 1`), which turns cross-origin calls into failed CORS
preflights; the cockpit frontend attaches it trivially, outsiders get 403.

## Tests (suite runs without pyodbc or az, as ever)

Spawner is an injected seam on `create_app`. Assert: sqlite URL → 409; spawner
receives exactly the fixed argv; second in-flight POST → `already_running`; flag
clears on (fake) process exit; missing az → error shape; missing header → 403;
`health.azure` true for `mssql+pyodbc`, false for sqlite.

## Docs

docs/cockpit.md "Pointing at Azure": button becomes the primary re-auth path,
terminal the fallback; troubleshooting table gains the expired-credential row.

## Explicitly not doing

Free-form URL entry (never — the chip must not print or accept connection strings),
the Local ⇄ Azure switcher (Phase 2 as designed), InteractiveBrowserCredential
(Stage-2 packaging), and any watching of `az login`'s exit code beyond clearing the
single-flight flag.

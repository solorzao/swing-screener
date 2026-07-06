# Cockpit Azure Sign-In Recovery Button — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When the cockpit's Azure DB is down/unauthenticated, the masthead DB chip offers a "Sign in to Azure" button that spawns `az login`; health polling detects recovery on its own.

**Architecture:** One new header-guarded, single-flight `POST /api/azure-login` route in the existing `create_app` factory (spawner injected as a test seam); `/api/health` grows an `azure` boolean; the React masthead chip grows a four-phase button (`idle → waiting → retry / no-cli`) driven purely by the existing 60s health poll. Design: `docs/plans/2026-07-06-cockpit-azure-login-design.md` (validated with Oliver).

**Tech Stack:** FastAPI + pytest/TestClient (backend, tests must run WITHOUT pyodbc or az), React 19 + TS strict (frontend), Vite build committed into `src/swing_screener/cockpit/static/`.

**Conventions that bind every task:** comments state contracts, not narration; error strings are class-name-only/leak-safe; the suite runs on a box with only `[dev,cockpit]` extras; frequent small commits on `feat/cockpit-azure-login`.

---

### Task 1: `_is_azure` helper + `azure` flag in `/api/health`

**Files:**
- Modify: `src/swing_screener/cockpit/api.py`
- Test: `tests/cockpit/test_api.py`

**Step 1: Write the failing test** (append to `tests/cockpit/test_api.py`)

```python
def test_health_carries_the_azure_flag(tmp_path: Path) -> None:
    # The flag describes the URL, not reachability: the frontend gates the sign-in
    # button on it, and an az login can never fix a sqlite file.
    assert _client(tmp_path).get("/api/health").json()["azure"] is False
    azure_client = TestClient(create_app(
        "mssql+pyodbc://@srv.database.windows.net/swing?driver=ODBC+Driver+18",
        edge_dir=tmp_path,
    ))
    body = azure_client.get("/api/health").json()
    assert body["azure"] is True
    # Still 200 and truthful even where pyodbc isn't installed (CI has no [azure]
    # extra): connectivity may be down, the flag must not care.
    assert body["label"] == "Azure SQL · swing"
```

**Step 2: Run it — must fail** with `KeyError: 'azure'`:
`.venv\Scripts\python -m pytest tests/cockpit/test_api.py::test_health_carries_the_azure_flag -q`

**Step 3: Implement.** In `api.py`, add after `connection_label`:

```python
def _is_azure(db_url: str) -> bool:
    """True iff the URL names an mssql database -- the only backend whose credential
    ``az login`` refreshes, and the gate for the sign-in affordance (design doc
    2026-07-06). URL-shaped, not connectivity-shaped: an unreachable Azure DB is
    exactly the case the button exists for."""
    try:
        return make_url(db_url).drivername.split("+", 1)[0] == "mssql"
    except Exception:  # unparseable: no affordance, same posture as connection_label
        return False
```

In `health()`, change the two returns to include the flag:

```python
            return {"connected": False, "label": label, "error": _down_summary(exc),
                    "azure": _is_azure(db_url)}
        return {"connected": True, "label": label, "error": None, "azure": _is_azure(db_url)}
```

**Step 4: Run the whole cockpit suite — all pass:**
`.venv\Scripts\python -m pytest tests/cockpit/ -q`

**Step 5: Commit:** `git add -A src/swing_screener/cockpit/api.py tests/cockpit/test_api.py && git commit -m "feat(cockpit): health reports whether the DB URL is Azure"`

---

### Task 2: `POST /api/azure-login` — guards, single-flight, spawner seam

**Files:**
- Modify: `src/swing_screener/cockpit/api.py`
- Test: `tests/cockpit/test_api.py`

**Step 1: Write the failing tests** (append; `_FakeProc` goes near the other helpers at the top):

```python
class _FakeProc:
    """Stands in for subprocess.Popen behind the spawner seam: poll() is the whole
    contract the endpoint reads."""
    def __init__(self) -> None:
        self.exited: int | None = None

    def poll(self) -> int | None:
        return self.exited


_HDR = {"X-Cockpit": "1"}
_AZURE_URL = "mssql+pyodbc://@srv.database.windows.net/swing?driver=ODBC+Driver+18"


def test_azure_login_requires_the_cockpit_header(tmp_path: Path) -> None:
    # Any webpage can fire a simple POST at localhost; the custom header forces a
    # failing CORS preflight cross-origin. No header -> 403 and NOTHING spawns.
    calls: list[int] = []
    client = TestClient(create_app(
        _AZURE_URL, edge_dir=tmp_path,
        login_spawner=lambda: calls.append(1) or _FakeProc(),
    ))
    assert client.post("/api/azure-login").status_code == 403
    assert calls == []


def test_azure_login_409s_on_a_local_database(tmp_path: Path) -> None:
    calls: list[int] = []
    client = TestClient(create_app(
        _db_url(tmp_path), edge_dir=tmp_path,
        login_spawner=lambda: calls.append(1) or _FakeProc(),
    ))
    assert client.post("/api/azure-login", headers=_HDR).status_code == 409
    assert calls == []


def test_azure_login_is_single_flight_until_the_process_exits(tmp_path: Path) -> None:
    procs: list[_FakeProc] = []

    def spawner() -> _FakeProc:
        procs.append(_FakeProc())
        return procs[-1]

    client = TestClient(create_app(_AZURE_URL, edge_dir=tmp_path, login_spawner=spawner))
    assert client.post("/api/azure-login", headers=_HDR).json() == {"started": True}
    # In-flight: a double-click must not open a second browser tab.
    assert client.post("/api/azure-login", headers=_HDR).json() == {
        "started": False, "already_running": True}
    assert len(procs) == 1
    procs[0].exited = 1  # the browser dance ended (success or not -- health decides)
    assert client.post("/api/azure-login", headers=_HDR).json() == {"started": True}
    assert len(procs) == 2


def test_azure_login_reports_a_missing_cli(tmp_path: Path) -> None:
    client = TestClient(create_app(
        _AZURE_URL, edge_dir=tmp_path, login_spawner=lambda: None))
    assert client.post("/api/azure-login", headers=_HDR).json() == {
        "started": False, "error": "az-not-found"}
```

**Step 2: Run — all four fail** (`login_spawner` unexpected kwarg / 404):
`.venv\Scripts\python -m pytest tests/cockpit/test_api.py -q -k azure_login`

**Step 3: Implement in `api.py`.**

Top of file: extend imports —

```python
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Protocol
```

Module level (near `_down_summary`):

```python
_LOGIN_TTL_S = 120.0  # matches the frontend's re-arm deadline (design doc 2026-07-06)


class _LoginProc(Protocol):
    """What the endpoint needs from a login process: liveness. Popen satisfies it;
    tests pass a fake -- the suite must run without spawning anything."""

    def poll(self) -> int | None: ...


def _spawn_az_login() -> _LoginProc | None:
    """Detached ``az login`` with a FIXED argv (never shell, nothing user-supplied
    ever reaches the command line) or None when the CLI is absent. Output goes to
    devnull: az drives the system browser itself and the cockpit may be console-less
    (pythonw). CREATE_NO_WINDOW keeps a cmd box from flashing over the window."""
    az = shutil.which("az")
    if az is None:
        return None
    flags = 0
    if sys.platform == "win32":  # attr exists only on Windows; Linux CI type-checks
        flags = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(  # noqa: S603 -- fixed argv by contract
        [az, "login"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
```

`create_app` signature gains the seam:

```python
def create_app(
    db_url: str,
    *,
    edge_dir: Path | None = None,
    static_dir: Path | None = None,
    login_spawner: Callable[[], _LoginProc | None] | None = None,
) -> FastAPI:
```

(and its docstring a line: `login_spawner` is the ``az login`` test seam; ``None`` spawns the real CLI.)

Inside `create_app`, before the static mount:

```python
    spawner = login_spawner if login_spawner is not None else _spawn_az_login
    login_lock = threading.Lock()
    login_flight: dict[str, object] = {"proc": None, "started": 0.0}

    def _login_active(now: float) -> bool:
        # poll() replaces a watcher thread (same observable contract, less
        # machinery); the TTL covers a wedged CLI that never exits.
        proc = login_flight["proc"]
        started = login_flight["started"]
        return (proc is not None and proc.poll() is None  # type: ignore[union-attr]
                and isinstance(started, float) and (now - started) < _LOGIN_TTL_S)

    @app.post("/api/azure-login")
    def azure_login(request: Request) -> dict[str, object]:
        """Spawn ``az login`` to refresh the AAD credential (design doc 2026-07-06).

        Contract: header-guarded (the custom header turns cross-origin calls into
        failed CORS preflights), Azure-mode only (a login cannot fix a sqlite file),
        single-flight, and NO success signal -- ``/api/health`` is the sole recovery
        oracle; the per-connection token fetch picks up the new credential on the
        next poll. Never touches the engine."""
        if request.headers.get("x-cockpit") != "1":
            raise HTTPException(status_code=403, detail="missing X-Cockpit header")
        if not _is_azure(db_url):
            raise HTTPException(status_code=409, detail="not an Azure database")
        with login_lock:
            if _login_active(time.monotonic()):
                return {"started": False, "already_running": True}
            proc = spawner()
            if proc is None:
                return {"started": False, "error": "az-not-found"}
            login_flight["proc"] = proc
            login_flight["started"] = time.monotonic()
        return {"started": True}
```

If mypy rejects the `dict[str, object]` juggling, prefer a tiny module-level
`@dataclass class _LoginFlight: proc: _LoginProc | None = None; started: float = 0.0`
with an `active(now)` method — same behavior, better types. Executor's call; keep it
typed with zero `Any`.

**Step 4: Run the cockpit suite + lints — all pass:**
`.venv\Scripts\python -m pytest tests/cockpit/ -q`
`.venv\Scripts\python -m ruff check src/swing_screener/cockpit tests/cockpit`
`.venv\Scripts\python -m mypy src/swing_screener/cockpit`

**Step 5: Commit:** `git commit -am "feat(cockpit): header-guarded single-flight POST /api/azure-login"`

---

### Task 3: Frontend — API mirror + masthead button + down-card hint

**Files:**
- Modify: `cockpit-ui/src/lib/api.ts`, `cockpit-ui/src/components/Masthead.tsx`, `cockpit-ui/src/App.tsx`, `cockpit-ui/src/index.css`

No vitest exists yet (queued Phase-2 item); `tsc -b` strict + oxlint are the checks.

**Step 1: `api.ts`.** Add `azure: boolean` to `Health` (doc comment: gates the sign-in
affordance). Generalize `fetchJson` to accept `init?: RequestInit` (pass straight to
`fetch`). Add:

```ts
export interface AzureLoginResponse {
  started: boolean
  already_running?: boolean
  error?: string
}

/** Kick off `az login` on the backend. The response only says whether a login
 * process STARTED — recovery is observed via /api/health, never via this call.
 * X-Cockpit is the guard header: it forces cross-origin callers into a failing
 * CORS preflight; same-origin us attaches it trivially. */
export const postAzureLogin = (): Promise<AzureLoginResponse> =>
  fetchJson<AzureLoginResponse>('/api/azure-login', {
    method: 'POST',
    headers: { 'X-Cockpit': '1' },
  })
```

**Step 2: `Masthead.tsx`.** Import `useEffect, useState` from react and
`postAzureLogin` from `../lib/api`. Inside the component:

```tsx
/* Sign-in phases (design doc 2026-07-06): the button renders ONLY when health says
   azure && !connected. 'waiting' is exited by health flipping connected (button
   unrenders) or by the deadline (re-arm as retry). No login-succeeded signal
   exists anywhere — health is the one recovery oracle. */
type LoginPhase = 'idle' | 'waiting' | 'retry' | 'no-cli'
const LOGIN_DEADLINE_MS = 120_000
```

```tsx
  const [phase, setPhase] = useState<LoginPhase>('idle')
  const connected = health !== null && health.connected

  useEffect(() => {
    if (connected) setPhase('idle') // recovered: next outage starts fresh
  }, [connected])

  useEffect(() => {
    if (phase !== 'waiting') return
    const id = setTimeout(() => setPhase('retry'), LOGIN_DEADLINE_MS)
    return () => clearTimeout(id)
  }, [phase])

  const onSignIn = () => {
    setPhase('waiting')
    postAzureLogin().then(
      (r) => {
        if (r.started || r.already_running) return // health decides from here
        setPhase(r.error === 'az-not-found' ? 'no-cli' : 'retry')
      },
      () => setPhase('retry'), // 403/409/network: chip already shows the down line
    )
  }
```

Render inside the existing `db-chip` span, after the label:

```tsx
        {health !== null && health.azure && !health.connected &&
          (phase === 'no-cli' ? (
            <span className="db-login-note">Azure CLI not installed</span>
          ) : (
            <button
              type="button"
              className="db-login"
              disabled={phase === 'waiting'}
              onClick={onSignIn}
            >
              {phase === 'waiting'
                ? 'waiting for browser sign-in…'
                : phase === 'retry'
                  ? 'Sign in to Azure — try again'
                  : 'Sign in to Azure'}
            </button>
          ))}
```

**Step 3: `App.tsx`.** The down-card hint points at the button when it exists:

```tsx
          <div className="db-down-hint">
            {health.data.azure
              ? 'The cockpit keeps retrying every 60 seconds — if your Azure sign-in expired, use “Sign in to Azure” in the masthead.'
              : 'The cockpit keeps retrying every 60 seconds; nothing below is lost.'}
          </div>
```

**Step 4: `index.css`.** After `.db-dot.none` (amber = attention-you-can-act-on;
an ENABLED masthead button is new — disarm is dead until Phase 3 — so it must read
as live, not like the dashed Phase-2 stubs):

```css
button.db-login {
  font-size: 10.5px;
  padding: 3px 9px;
  border-radius: 3px;
  background: color-mix(in srgb, var(--amber) 12%, var(--panel2));
  border: 1px solid color-mix(in srgb, var(--amber) 55%, var(--line2));
  color: var(--amber);
  cursor: pointer;
  white-space: nowrap;
}

button.db-login:disabled {
  color: var(--dim);
  border-color: var(--line2);
  background: transparent;
  cursor: wait;
}

.db-login-note {
  font-size: 10.5px;
  color: var(--dim);
  white-space: nowrap;
}
```

**Step 5: Type-check + lint + build (build also regenerates `static/`):**
`cd cockpit-ui && npm run lint && npm run build`
Expected: oxlint clean; tsc clean; Vite writes `../src/swing_screener/cockpit/static/`.

**Step 6: Commit source AND the rebuilt committed static:**
`git add cockpit-ui/src src/swing_screener/cockpit/static && git commit -m "feat(cockpit-ui): Sign in to Azure button on the DB chip"`

---

### Task 4: Docs + verification + PR

**Files:**
- Modify: `docs/cockpit.md`

**Step 1:** In "Pointing at Azure", after the firewall/read-only paragraph, add:

```markdown
When the Azure credential expires, the DB chip turns red and grows a **Sign in to
Azure** button — it runs `az login` (system browser) and the chip recovers on the
next health poll; nothing else to do. Terminal `az login` still works as the
fallback, and the button only exists in Azure mode (it can't fix a local file).
```

Add a troubleshooting row/paragraph: chip red + `database unreachable (…)` in Azure
mode → credential expired → the button, or `az login` in any terminal.

**Step 2: Full gate, matching CI:**
`.venv\Scripts\python -m pytest tests/ -q` (full suite)
`.venv\Scripts\python -m ruff check src tests`
`.venv\Scripts\python -m mypy src`

**Step 3: Live smoke (the real gesture, both modes):**
- `python -m swing_screener.cockpit --browser --db sqlite:///local.db` → no button
  when down/up (local mode), POST `/api/azure-login` with header → 409, without → 403.
- Relaunch `pythonw -m swing_screener.cockpit` (env var → Azure, healthy) → green
  chip, NO button. (Deliberately not testing a real expired token — the fake-spawner
  tests own that path.)

**Step 4: Commit docs, push, PR** onto `feat/cockpit-azure-login`, PR body links the
design doc; then `gh pr checks --watch`.

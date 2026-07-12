"""Helpers shared by ``cockpit.api`` and the ``cockpit.routers`` modules.

Split mechanically out of ``cockpit/api.py`` (2026-07-11, post-Task 9); every
function body is unchanged. Import direction is one-way: routers import from
here, ``api.py`` imports from both -- nothing here imports a router or the app
factory, so there is no cycle to trip over.
"""

import math
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException, Request
from sqlalchemy import make_url, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from swing_screener.cockpit.livedata import Snapshot
from swing_screener.db.session import get_engine
from swing_screener.pipeline.disarm import _STOP_TYPES


def connection_label(db_url: str) -> str:
    """Human-readable DB target WITHOUT host or credentials (the ported chip semantics).

    'sqlite:///C:/data/swing.db'                            -> 'Local SQLite · swing.db'
    'mssql+pyodbc://u:p@srv.database.windows.net/swing?...' -> 'Azure SQL · swing'
    unparseable                                             -> 'Database'
    """
    try:
        u = make_url(db_url)
    except Exception:  # unparseable input: still never echo it back
        return "Database"
    driver = u.drivername.split("+", 1)[0]
    if driver == "sqlite":
        db = u.database
        if not db or db == ":memory:":
            return "Local SQLite"
        return f"Local SQLite · {Path(db).name}"  # filename only, never the directory
    if driver == "mssql":
        return f"Azure SQL · {u.database or '?'}"
    return driver


def _is_azure(db_url: str) -> bool:
    """True iff the URL names an mssql database -- the only backend whose credential
    ``az login`` refreshes, and the gate for the sign-in affordance (design doc
    2026-07-06). URL-shaped, not connectivity-shaped: an unreachable Azure DB is
    exactly the case the button exists for."""
    try:
        return make_url(db_url).drivername.split("+", 1)[0] == "mssql"
    except Exception:  # unparseable: no affordance, same posture as connection_label
        return False


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


@dataclass
class _LoginFlight:
    """The one in-flight ``az login``; read and mutated only under the app's login
    lock, so plain attributes are race-free."""

    proc: _LoginProc | None = None
    started: float = 0.0

    def active(self, now: float) -> bool:
        # poll() replaces a watcher thread (same observable contract, less
        # machinery); the TTL covers a wedged CLI that never exits.
        return (
            self.proc is not None
            and self.proc.poll() is None
            and (now - self.started) < _LOGIN_TTL_S
        )


def _require_cockpit(request: Request) -> None:
    """The one mutation guard, as a dependency: the custom header can't ride a
    cross-origin simple request, so any webpage's form/fetch dies in the CORS
    preflight; a request without it is a 403 before ANY other dependency runs
    (attached via the decorator's ``dependencies=[...]``, which FastAPI solves
    ahead of parameter dependencies -- no model validation, no engine touch; the
    raw JSON decode still precedes the guard, so a syntactically-broken body 422s
    first -- harmless, nothing side-effectful runs). Ordering pin: headerless +
    invalid-FIELDS body -> 403, never 422. Every new action endpoint takes the
    guard by copying one decorator argument."""
    if request.headers.get("x-cockpit") != "1":
        raise HTTPException(status_code=403, detail="missing X-Cockpit header")


def _json_safe_floats(obj: object) -> object:
    """Replace non-finite floats with their string form (``'inf'``/``'-inf'``/
    ``'nan'``), recursively -- everything else passes through untouched. Applied to
    the 422 error payload AFTER ``jsonable_encoder`` (which has already reduced it
    to dicts/lists/primitives), so only the offending input echoes change shape."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe_floats(v) for v in obj]
    return obj


def _utc_iso(value: datetime | None) -> str | None:
    """A stored datetime as an unambiguous UTC wire string ('+00:00'-suffixed).

    DB datetimes come back tz-naive (sqlite/mssql DATETIME drop tzinfo); served
    naive, JS's ``Date()`` would parse them as LOCAL time and skew every
    relative-time render by the zone offset. The cockpit/worker writers stamp
    UTC, so stamping UTC here is correct; legacy Streamlit rows were naive LOCAL
    and wear a bounded display offset until they age out (disclosed at the
    endpoints). Moved here from ``routers/analysis.py`` (verbatim) once the
    reference router became its second consumer."""
    return value.replace(tzinfo=UTC).isoformat() if value is not None else None


def _down_summary(exc: Exception) -> str:
    """One safe line for a dead DB: the exception CLASS only -- driver messages can
    embed the file path or DSN, so the message itself never reaches a response."""
    return f"database unreachable ({type(exc).__name__})"


def build_engine_seams(
    db_url: str,
) -> tuple[Callable[[], Engine], Callable[[], Iterator[Session]]]:
    """The per-app engine accessor and session dependency, as one pair of closures
    over one lazily-created engine (moved verbatim from ``create_app``; the seams
    are handed to every router factory so all endpoints share the single cache)."""
    engine_cache: list[Engine] = []
    engine_lock = threading.Lock()

    def _engine() -> Engine:
        # Cached only on success so a down DB is re-probed per request, not latched.
        # Locked: uvicorn runs sync endpoints on a threadpool and the frontend fires
        # its first requests concurrently, so the empty-cache check-and-append races.
        with engine_lock:
            if not engine_cache:
                engine_cache.append(get_engine(db_url))
            return engine_cache[0]

    def _session() -> Iterator[Session]:
        # Engine creation + connectivity probe stay OUTSIDE the yield's try-scope:
        # a broad except wrapped around the yield would also swallow endpoint errors
        # (they propagate back through generator dependencies).
        try:
            engine = _engine()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=_down_summary(exc)) from exc
        with Session(engine) as session:
            try:
                session.execute(text("SELECT 1"))
            except Exception as exc:
                raise HTTPException(status_code=503, detail=_down_summary(exc)) from exc
            yield session

    return _engine, _session


def _armed_symbols(snapshot: Snapshot | None) -> frozenset[str]:
    """Symbols holding a live protective sell order at the venue -- ``order_type``
    in disarm's ``_STOP_TYPES`` (imported, so the two definitions cannot drift).
    Empty for a None snapshot: the caller's lamp answers ``unknown`` there."""
    if snapshot is None:
        return frozenset()
    return frozenset(o.symbol for o in snapshot.open_orders
                     if o.side == "sell" and o.order_type in _STOP_TYPES)


def _bracket(ticker: str, stop: float | None, *, snapshot: Snapshot | None,
             armed: frozenset[str]) -> str:
    """One row's bracket lamp (per-kind semantics in the ``positions`` docstring):
    no snapshot -> ``unknown``; a venue-held stop for the symbol -> ``armed``; a
    recorded DB stop -> ``db-only``; ``unprotected`` only with NO recorded stop --
    unreachable today (both stop columns are NOT NULL) but kept as the wire
    contract's honest floor."""
    if snapshot is None:
        return "unknown"
    if ticker in armed:
        return "armed"
    return "db-only" if stop is not None else "unprotected"

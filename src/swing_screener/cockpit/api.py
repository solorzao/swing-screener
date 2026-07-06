"""The cockpit's HTTP API: an app factory serving health, heartbeats, and Stats.

Three constraints, stated as contract:

* Every statistic leaves this API as a full ``Stat`` dict -- value plus n, n_clusters,
  both CI bounds, cost level, corpus id, facet (docs/plans/2026-07-05-desktop-ui-design.md,
  "The three mechanical rules", rule 1: the frontend has NO renderer for a bare float,
  so a number without provenance is unrepresentable).
* The connection label NEVER contains the URL, host, or credentials -- the Streamlit
  sidebar chip's guarantee (``dashboard/ui.py connection_label``), reimplemented here
  rather than imported because that module is slated for deletion with the dashboard.
* A dead database is a friendly answer, never a traceback: ``/api/health`` always
  answers 200 with ``connected: false`` plus a one-line summary; data endpoints answer
  503 with a JSON ``detail``. That covers MID-REQUEST failures too: a stale-schema
  local.db passes the SELECT-1 probe and then raises inside the endpoint, so an
  app-level ``SQLAlchemyError`` handler translates those to the same 503 posture.
  No stack trace and no URL in any response body.
"""

import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import make_url, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import PerformanceSummary, breakdown
from swing_screener.cockpit.heartbeats import Heartbeat, collect_heartbeats
from swing_screener.cockpit.stats import stat_from_summary
from swing_screener.db.repo import load_research_paper_trades
from swing_screener.db.session import get_engine


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


def create_app(
    db_url: str,
    *,
    edge_dir: Path | None = None,
    static_dir: Path | None = None,
    login_spawner: Callable[[], _LoginProc | None] | None = None,
) -> FastAPI:
    """Build the cockpit API around one database URL.

    The engine is created lazily (per app, on first use) so an unreachable database
    surfaces per-request as ``connected: false`` / 503 -- never as a factory-time crash.
    ``app.state.db_url`` is stored for the pywebview launcher. ``edge_dir`` is the
    heartbeats test seam: where ``collect_heartbeats`` looks for edge files; ``None``
    resolves via settings. ``login_spawner`` is the ``az login`` test seam; ``None``
    spawns the real CLI.

    ``static_dir`` (default: the packaged ``cockpit/static/``, built by the Vite
    frontend) is mounted at ``/`` AFTER the API routes, so ``/api/*`` always wins.
    When ``index.html`` is absent -- a clone before Task 6, or a broken build --
    ``/`` answers 200 with a JSON pointer instead: a missing frontend is a setup
    state, not a server error, so it must not read as one.
    """
    app = FastAPI(title="swing-screener cockpit")
    app.state.db_url = db_url

    @app.exception_handler(SQLAlchemyError)
    def _database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        """Mid-request DB failures must not 500: a stale-schema local.db (e.g. a
        column added after the file was created) passes the SELECT-1 probe, then
        raises OperationalError inside the endpoint query. Same leak posture as
        ``_down_summary`` -- class name only, the driver message can embed the SQL,
        the file path, or the DSN. Distinct wording ('error', not 'unreachable')
        so the two failure shapes stay tellable-apart in the UI."""
        return JSONResponse(
            status_code=503,
            content={"detail": f"database error ({type(exc).__name__})"},
        )

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

    @app.get("/api/health")
    def health() -> dict[str, object]:
        """Connectivity + safe label. Always 200; ``connected`` carries the truth."""
        label = connection_label(db_url)
        try:
            with _engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            return {"connected": False, "label": label, "error": _down_summary(exc),
                    "azure": _is_azure(db_url)}
        return {"connected": True, "label": label, "error": None, "azure": _is_azure(db_url)}

    @app.get("/api/heartbeats")
    def heartbeats(session: Session = Depends(_session)) -> list[dict[str, object]]:
        """Every job's pulse, evaluated against the wall clock at request time."""
        beats = collect_heartbeats(session, now=datetime.now(UTC), edge_dir=edge_dir)
        return [_beat_dict(b) for b in beats]

    @app.get("/api/stats/cohorts")
    def cohort_stats(session: Session = Depends(_session)) -> dict[str, object]:
        """Research-grid cohort expectancies; every number is a full Stat dict (rule 1).

        Shape (stable contract): ``{"cohorts": [{"key": <play_type>, "strength":
        <str | null>, "stat": {<10-key Stat>}}, ...]}``, play_type-major: each
        play_type's aggregate row first (``strength: null``), then its per-strength
        split, both alphabetically. Strength-split keys come from ``breakdown``'s
        ``str()`` coercion, so a null-strength cohort appears as the string ``"None"``
        -- distinct from the aggregate row's ``null``. ``cost_level``/``corpus_id``
        are an explicit null (not persisted yet); facet is ``"research"``.
        """
        trades = load_research_paper_trades(session)
        by_play = breakdown(trades, "play_type")
        cohorts: list[dict[str, object]] = []
        for play_type in sorted(by_play):
            cohorts.append(_cohort(play_type, None, by_play[play_type]))
            subset = [t for t in trades if str(t.play_type) == play_type]
            by_strength = breakdown(subset, "strength")
            cohorts.extend(
                _cohort(play_type, strength, by_strength[strength])
                for strength in sorted(by_strength)
            )
        return {"cohorts": cohorts}

    spawner = login_spawner if login_spawner is not None else _spawn_az_login
    login_lock = threading.Lock()
    login_flight = _LoginFlight()

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
            if login_flight.active(time.monotonic()):
                return {"started": False, "already_running": True}
            proc = spawner()
            if proc is None:
                return {"started": False, "error": "az-not-found"}
            login_flight.proc = proc
            login_flight.started = time.monotonic()
        return {"started": True}

    resolved_static = static_dir if static_dir is not None else Path(__file__).parent / "static"
    if (resolved_static / "index.html").is_file():
        # html=True serves index.html at "/" and falls through to real asset files.
        app.mount("/", StaticFiles(directory=resolved_static, html=True), name="static")
    else:

        @app.get("/")
        def frontend_missing() -> dict[str, str]:
            """Friendly setup pointer while ``static/`` has no build (see factory doc)."""
            return {
                "detail": "frontend not built -- run `npm run build` in cockpit-ui/ "
                "to populate swing_screener/cockpit/static/"
            }

    return app


def _down_summary(exc: Exception) -> str:
    """One safe line for a dead DB: the exception CLASS only -- driver messages can
    embed the file path or DSN, so the message itself never reaches a response."""
    return f"database unreachable ({type(exc).__name__})"


def _cohort(key: str, strength: str | None, summary: PerformanceSummary) -> dict[str, object]:
    stat = stat_from_summary(summary, cost_level=None, corpus_id=None, facet="research")
    return {"key": key, "strength": strength, "stat": stat.as_dict()}


def _beat_dict(b: Heartbeat) -> dict[str, object]:
    """Explicit wire form -- ``Heartbeat`` has no ``as_dict`` by design, so the
    serialization contract (ISO-8601-or-null ``last``) lives here, visibly."""
    return {
        "name": b.name,
        "state": b.state,
        "last": b.last.isoformat() if b.last is not None else None,
        "period_s": b.period_s,
        "grace_s": b.grace_s,
        "detail": b.detail,
    }

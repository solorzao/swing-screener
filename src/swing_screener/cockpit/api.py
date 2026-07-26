"""The cockpit's HTTP API: an app factory serving health, heartbeats, and Stats.

Three constraints, stated as contract:

* Every statistic leaves this API as a full ``Stat`` dict -- value plus n, n_clusters,
  both CI bounds, cost level, corpus id, facet (docs/plans/2026-07-05-desktop-ui-design.md,
  "The three mechanical rules", rule 1: the frontend has NO renderer for a bare float,
  so a number without provenance is unrepresentable).
* The connection label NEVER contains the URL, host, or credentials -- the guarantee the
  retired Streamlit sidebar chip carried (its ``connection_label``), reimplemented here
  rather than carried over from the now-deleted dashboard module.
* A dead database is a friendly answer, never a traceback: ``/api/health`` always
  answers 200 with ``connected: false`` plus a one-line summary; data endpoints answer
  503 with a JSON ``detail``. That covers MID-REQUEST failures too: a stale-schema
  local.db passes the SELECT-1 probe and then raises inside the endpoint, so an
  app-level ``SQLAlchemyError`` handler translates those to the same 503 posture.
  No stack trace and no URL in any response body.

Since the 2026-07-11 split the endpoints themselves live in ``cockpit/routers/*``
(one ``build_<name>_router`` per cluster, bodies moved verbatim) with shared helpers
in ``cockpit/common.py``; this module builds the seams and assembles the app.
"""

import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from swing_screener.cockpit.common import (
    ActionNonce,
    _json_safe_floats,
    _LoginProc,
    _spawn_az_login,
    build_engine_seams,
)
from swing_screener.cockpit.gh import latest_workflow_run
from swing_screener.cockpit.livedata import BrokerSnapshot, QuoteCache
from swing_screener.cockpit.routers.analysis import (
    _STALE_AFTER,  # noqa: F401 -- re-export: the lockstep test pins api._STALE_AFTER
    build_analysis_router,
)
from swing_screener.cockpit.routers.analyst import build_analyst_router
from swing_screener.cockpit.routers.books import build_books_router
from swing_screener.cockpit.routers.events import build_events_router
from swing_screener.cockpit.routers.journal import build_journal_router
from swing_screener.cockpit.routers.gex import build_gex_router
from swing_screener.cockpit.routers.lab import LabBars, build_lab_router
from swing_screener.cockpit.routers.coach import build_coach_router
from swing_screener.cockpit.routers.audit import build_audit_router
from swing_screener.cockpit.routers.picks import build_picks_router
from swing_screener.cockpit.routers.playbooks import build_playbooks_router
from swing_screener.cockpit.routers.proposals import build_proposals_router
from swing_screener.cockpit.routers.reference import build_reference_router
from swing_screener.cockpit.routers.safety import build_safety_router
from swing_screener.cockpit.routers.scoreboard import build_scoreboard_router
from swing_screener.cockpit.routers.strategies import build_strategies_router
from swing_screener.cockpit.routers.trades import build_trades_router
from swing_screener.cockpit.routers.weather import build_weather_router
from swing_screener.data import quotes
from swing_screener.options.run import Snapshotter
from swing_screener.pipeline.broker import BrokerClient
from swing_screener.pipeline.broker_alpaca import build_broker
from swing_screener.settings import load_settings


def create_app(
    db_url: str,
    *,
    edge_dir: Path | None = None,
    static_dir: Path | None = None,
    login_spawner: Callable[[], _LoginProc | None] | None = None,
    latest_closes_fn: Callable[[list[str]], dict[str, float]] | None = None,
    broker_factory: Callable[[], BrokerClient | None] | None = None,
    gex_snapshotter: Snapshotter | None = None,
    gex_daily_bars: Callable[[str], object] | None = None,
    lab_bars: LabBars | None = None,
    lab_worker: Callable[[int], None] | None = None,
) -> FastAPI:
    """Build the cockpit API around one database URL.

    The engine is created lazily (per app, on first use) so an unreachable database
    surfaces per-request as ``connected: false`` / 503 -- never as a factory-time crash.
    ``app.state.db_url`` is stored for the pywebview launcher. ``edge_dir`` is the
    heartbeats test seam: where ``collect_heartbeats`` looks for edge files; ``None``
    resolves via settings. ``login_spawner`` is the ``az login`` test seam; ``None``
    spawns the real CLI.

    ``latest_closes_fn`` / ``broker_factory`` are the livedata test seams: ``None``
    binds the real ``data.quotes.latest_closes`` (over the settings bar-cache dir)
    and the real ``build_broker`` resolution. Each is wrapped in its TTL cache
    (``cockpit.livedata``) and parked on ``app.state.quote_cache`` /
    ``app.state.broker_snapshot`` -- Tasks 6/9/10 consume them from there; nothing
    in THIS factory calls them, so building an app never touches yfinance or a venue.

    ``static_dir`` (default: the packaged ``cockpit/static/``, built by the Vite
    frontend) is mounted at ``/`` AFTER the API routes, so ``/api/*`` always wins.
    When ``index.html`` is absent -- a clone before Task 6, or a broken build --
    ``/`` answers 200 with a JSON pointer instead: a missing frontend is a setup
    state, not a server error, so it must not read as one.

    The GH workflow poller is opt-in: ``SWING_GH_TOKEN`` + ``SWING_GH_REPO``
    (both, read once here) wire ``cockpit.gh`` into the heartbeats; with either
    missing the three GH beats stay explicit UNKNOWN placeholders.
    """
    app = FastAPI(title="swing-screener cockpit")
    app.state.db_url = db_url

    gh_token = os.environ.get("SWING_GH_TOKEN")
    gh_repo = os.environ.get("SWING_GH_REPO")
    gh_latest = _gh_poller(gh_repo, gh_token) if gh_token and gh_repo else None

    # Livedata instances (Phase 3 Task 4): constructed here, consumed by the
    # routers AND parked on app.state (the FastAPI-idiomatic home for per-app
    # singletons -- tests and the launcher reach them there). Construction is
    # inert -- neither cache calls upstream until a consumer asks. The resolved
    # factory is ALSO the action endpoints' seam: DISARM needs a live client (a
    # cached snapshot is a read, never something to cancel through), and the
    # safety report resolves a fresh client per request.
    resolved_broker_factory = (
        broker_factory if broker_factory is not None else _default_broker_factory
    )
    quote_cache = QuoteCache(
        latest_closes_fn if latest_closes_fn is not None else _default_quote_fetch()
    )
    broker_snapshot = BrokerSnapshot(resolved_broker_factory)
    app.state.quote_cache = quote_cache
    app.state.broker_snapshot = broker_snapshot

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

    @app.exception_handler(RequestValidationError)
    def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """FastAPI's stock 422, with non-finite input echoes made serializable: the
        default handler echoes each error's ``input`` verbatim, and a hand-crafted
        JSON ``Infinity``/``NaN`` body (which ``json.loads`` accepts) makes that
        echo itself unserializable -- Starlette's JSONResponse pins
        ``allow_nan=False`` -- so the 422 would collapse into a 500. A validation
        REJECTION must never read as a server error (see ``_json_safe_floats``)."""
        return JSONResponse(
            status_code=422,
            content={"detail": _json_safe_floats(jsonable_encoder(exc.errors()))},
        )

    _engine, _session = build_engine_seams(db_url)

    # The DISARM single-flight lock (mirrors the books router's login_lock).
    # Parked on app.state so a test can hold it deterministically instead of
    # racing two threads.
    disarm_lock = threading.Lock()
    app.state.disarm_lock = disarm_lock

    # The post-action wake nonce (Task 12): one per app, threaded to every
    # mutating router AND the events router -- a successful action POST bumps
    # it, the SSE change token carries it, so other windows wake within one
    # token-poll tick. Parked on app.state so tests can read (and bump) it.
    action_nonce = ActionNonce()
    app.state.action_nonce = action_nonce

    spawner = login_spawner if login_spawner is not None else _spawn_az_login

    app.include_router(build_books_router(
        db_url=db_url, _engine=_engine, _session=_session, edge_dir=edge_dir,
        gh_latest=gh_latest, spawner=spawner, action_nonce=action_nonce,
    ))
    app.include_router(build_safety_router(
        _session=_session, edge_dir=edge_dir,
        resolved_broker_factory=resolved_broker_factory,
        broker_snapshot=broker_snapshot, disarm_lock=disarm_lock,
        action_nonce=action_nonce,
    ))
    app.include_router(build_trades_router(
        _session=_session, quote_cache=quote_cache, broker_snapshot=broker_snapshot,
        action_nonce=action_nonce,
    ))
    app.include_router(build_analysis_router(
        _session=_session, db_url=db_url, action_nonce=action_nonce,
    ))
    app.include_router(build_picks_router(_session=_session, quote_cache=quote_cache))
    app.include_router(build_scoreboard_router(_session=_session))
    app.include_router(build_reference_router(_session=_session))
    app.include_router(build_proposals_router(
        edge_dir=edge_dir, action_nonce=action_nonce,
    ))
    app.include_router(build_playbooks_router(_session=_session, edge_dir=edge_dir))
    app.include_router(build_strategies_router(_session=_session, edge_dir=edge_dir))
    app.include_router(build_weather_router(_session=_session))
    app.include_router(build_analyst_router(_session=_session))
    app.include_router(build_journal_router(
        _session=_session, action_nonce=action_nonce,
    ))
    app.include_router(build_gex_router(
        _session=_session, action_nonce=action_nonce,
        snapshotter=gex_snapshotter, daily_bars=gex_daily_bars,
    ))
    app.include_router(build_lab_router(
        _engine=_engine, _session=_session, action_nonce=action_nonce,
        bars=lab_bars, worker=lab_worker,
    ))
    app.include_router(build_coach_router(
        _session=_session, action_nonce=action_nonce,
    ))
    app.include_router(build_audit_router(
        _session=_session, action_nonce=action_nonce,
    ))
    app.include_router(build_events_router(
        _engine=_engine, edge_dir=edge_dir, action_nonce=action_nonce,
    ))

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


def _gh_poller(repo: str, token: str) -> Callable[[str], tuple[datetime, str] | None]:
    """Bind the GH poller to one repo + token (read once at create_app time). A
    named closure rather than a lambda over the env reads: mypy's Optional
    narrowing does not survive into a nested scope, so the strings are re-bound
    as parameters here. Failure posture lives in ``gh.latest_workflow_run``
    itself (any error -> None -> UNKNOWN beat)."""

    def poll(workflow: str) -> tuple[datetime, str] | None:
        return latest_workflow_run(repo, workflow, token)

    return poll


def _default_quote_fetch() -> Callable[[list[str]], dict[str, float]]:
    """Bind ``data.quotes.latest_closes`` over the settings bar-cache dir. The
    cache_dir is read ONCE, at create_app time -- it is env-backed and stable for
    the life of the process, so a per-call ``load_settings()`` would buy nothing.
    ``quotes.latest_closes`` resolves at CALL time (module attribute), keeping the
    documented monkeypatch seam in ``data/quotes.py`` intact."""
    cache_dir = load_settings().cache_dir

    def fetch(tickers: list[str]) -> dict[str, float]:
        return quotes.latest_closes(tickers, cache_dir=cache_dir)

    return fetch


def _default_broker_factory() -> BrokerClient | None:
    """The real broker resolution -- the same ``build_broker(load_settings())``
    one-liner every pipeline entrypoint uses. Settings are read at CALL time (each
    snapshot refresh; they are cheap env reads) BY CHOICE, not accident: broker /
    credential state follows the entrypoints' resolve-at-use pattern, unlike
    ``_default_quote_fetch``'s cache_dir, which is process-stable and bound once.
    No broker configured -> None, which ``BrokerSnapshot`` caches as a None
    snapshot for its TTL."""
    return build_broker(load_settings())

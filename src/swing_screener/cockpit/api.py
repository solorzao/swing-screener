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

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

import anyio.to_thread
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, make_url, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from swing_screener.analytics.performance import (
    SCORE_EDGES,
    PerformanceSummary,
    _bucket_trades_by_rank,
    _bucket_trades_by_score,
    breakdown,
    cost_level_for,
    equity_curve,
    leaderboard_flag,
    leaderboard_order,
    paired_arm_delta,
    score_stamped,
    summarize,
)
from swing_screener.cockpit.gh import latest_workflow_run
from swing_screener.cockpit.heartbeats import (
    Heartbeat,
    collect_heartbeats,
    newest_verdicts_mtime,
)
from swing_screener.cockpit.livedata import BrokerSnapshot, QuoteCache
from swing_screener.cockpit.settlement import (
    STATES,
    SettlementCard,
    build_cards,
    facet_filter,
)
from swing_screener.cockpit.stats import stat_from_paired_delta, stat_from_summary
from swing_screener.data import quotes
from swing_screener.db.models import (
    AnalystCall,
    EmailLog,
    ExitEvent,
    MarketReport,
    PaperTrade,
    Signal,
)
from swing_screener.db.repo import (
    latest_reversal_funnel,
    load_closed_paper_trades,
    load_research_paper_trades,
)
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.autonomy import autonomy_gate, gate_countdown
from swing_screener.pipeline.broker import BrokerClient
from swing_screener.pipeline.broker_alpaca import build_broker
from swing_screener.pipeline.registry import load_experiments
from swing_screener.pipeline.variants import DEFAULT_VARIANT
from swing_screener.settings import load_settings, resolve_edge_dir


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
    latest_closes_fn: Callable[[list[str]], dict[str, float]] | None = None,
    broker_factory: Callable[[], BrokerClient | None] | None = None,
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

    # Livedata instances (Phase 3 Task 4): constructed here, consumed by Tasks
    # 6/9/10 from app.state (the FastAPI-idiomatic home for per-app singletons).
    # Construction is inert -- neither cache calls upstream until a consumer asks.
    app.state.quote_cache = QuoteCache(
        latest_closes_fn if latest_closes_fn is not None else _default_quote_fetch()
    )
    app.state.broker_snapshot = BrokerSnapshot(
        broker_factory if broker_factory is not None else _default_broker_factory
    )

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
        beats = collect_heartbeats(
            session, now=datetime.now(UTC), edge_dir=edge_dir, gh_latest=gh_latest
        )
        return [_beat_dict(b) for b in beats]

    @app.get("/api/stats/cohorts")
    def cohort_stats(
        facet: Literal["research", "gold"] = "research",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """Research-grid cohort expectancies; every number is a full Stat dict (rule 1).

        Shape (stable contract): ``{"cohorts": [{"key": <play_type>, "strength":
        <str | null>, "stat": {<10-key Stat>}}, ...]}``, play_type-major: each
        play_type's aggregate row first (``strength: null``), then its per-strength
        split, both alphabetically. Strength-split keys come from ``breakdown``'s
        ``str()`` coercion, so a null-strength cohort appears as the string ``"None"``
        -- distinct from the aggregate row's ``null``. ``cost_level`` is derived per
        cohort SUBSET by ``cost_level_for``'s cutoff rule: ``"0.05"`` iff every closed
        trade provably exited on/after the cost epoch, else an honest null (one
        pre-cutoff exit poisons its whole cohort). ``corpus_id`` stays an explicit
        null (not persisted yet). ``facet`` selects the book -- ``gold`` keeps only
        would_surface-truthy rows -- and is echoed on every Stat; anything else is
        FastAPI's 422 via the ``Literal``.
        """
        trades = facet_filter(load_research_paper_trades(session), facet)
        by_play = breakdown(trades, "play_type")
        cohorts: list[dict[str, object]] = []
        for play_type in sorted(by_play):
            subset = [t for t in trades if str(t.play_type) == play_type]
            cohorts.append(_cohort(play_type, None, by_play[play_type], subset, facet))
            by_strength = breakdown(subset, "strength")
            cohorts.extend(
                _cohort(play_type, strength, by_strength[strength],
                        [t for t in subset if str(t.strength) == strength], facet)
                for strength in sorted(by_strength)
            )
        return {"cohorts": cohorts}

    @app.get("/api/forward-books")
    def forward_books(
        facet: Literal["research", "gold"] = "research",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """One settlement card per registered experiment (edge/experiments.json).

        Cards order decision-forcing first: awaiting-decision (settled or futile),
        then accruing, then retired -- stable by name within each group. The ordering
        is a presentation concern, so it lives here, not in settlement's math. A
        missing or empty registry is a normal setup state: ``{"cards": []}``, never
        an error. ``facet`` threads through to ``build_cards`` (it filters each
        loaded book internally, before any math). Budget: ~1.1s of bootstrap math per
        request at the real 9-experiment registry shape -- ~2% duty cycle at the
        frontend's 60s poll; revisit (cache across requests) if wall time approaches
        the poll interval or a second polling client appears.
        """
        experiments = load_experiments(resolve_edge_dir(edge_dir))

        # Per-request memo: the registry shares books heavily (every arm card hydrates
        # the identical default-variant pool; variant cards share the default control),
        # so the distinct loads are roughly half the raw count. Copied on the way out
        # so no consumer can mutate a list another card is about to read.
        books: dict[tuple[str | None, str | None, str], list[PaperTrade]] = {}

        def _load(
            *, play_type: str | None, arm: str | None, variant: str
        ) -> list[PaperTrade]:
            key = (play_type, arm, variant)
            if key not in books:
                books[key] = load_closed_paper_trades(
                    session, play_type=play_type, arm=arm, variant=variant
                )
            return list(books[key])

        cards = build_cards(
            experiments, book_loader=_load, now=datetime.now(UTC), facet=facet
        )
        cards.sort(key=lambda c: (_STATE_RANK[c.state], c.name))
        return {"cards": [_card_dict(c) for c in cards]}

    @app.get("/api/funnel")
    def funnel(session: Session = Depends(_session)) -> dict[str, object]:
        """The latest daily digest's reversal funnel snapshot -- the row with the
        newest ``run_date``; ``{"funnel": null}`` before the first digest records
        one. ``overflow`` is the comma-joined column split back into a ticker list,
        empties dropped (the empty string means "no overflow", never ``[""]``)."""
        row = latest_reversal_funnel(session)
        if row is None:
            return {"funnel": None}
        return {"funnel": {
            "run_date": row.run_date.isoformat(),
            "detected": row.detected,
            "confirmed": row.confirmed,
            "fresh": row.fresh,
            "actionable": row.actionable,
            "surfaced": row.surfaced,
            "overflow": [t for t in row.overflow_tickers.split(",") if t],
            "pool_n": row.pool_n,
            "confirmed_only": row.confirmed_only,
            "premium_only": row.premium_only,
            "already_ran_checked": row.already_ran_checked,
        }}

    @app.get("/api/stats/performance")
    def performance_stats(
        play_type: Literal["all", "continuation", "reversal"] = "all",
        window: Literal["all", "90", "180", "365"] = "all",
        facet: Literal["research", "gold"] = "research",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """The Streamlit Screener Performance page as data -- every aggregate a Stat.

        Replicates the retired Streamlit Screener Performance page's load-bearing order
        EXACTLY: (1) ``facet`` (``facet_filter``) then the ``play_type`` filter over
        the research grid; (2) the strategy leaderboard over the ``arm == BASELINE``
        subset, with the trailing ``window`` cut (days back from today, on
        ``opened_date`` -- an undated row drops from windowed views) applied to THAT
        subset; the window scopes ONLY the leaderboard, like the page's segmented
        control; (3) everything downstream -- arms, KPIs, breakdowns, equity curve --
        scopes to ``variant == DEFAULT_VARIANT`` (the exit-arm A/B is only honest
        within one screen variant), with the page's arm-detail radio pinned at its
        default (the BASELINE arm when several arms exist).

        Where the page HID degenerate sections (single variant / single arm / < 2
        populated score bands), the API always returns every key with whatever data
        exists (empty lists when there is none) -- the FRONTEND decides rendering.
        The one data-shaping exception: score bands with no closes are omitted so
        they don't read as spurious zeros (rank buckets keep their fixed labels,
        page parity). The score cut goes through ``score_stamped`` (forward book
        only); regime cuts skip unknown-regime rows. ``profit_factor`` serializes an
        all-winner book as null -- JSON has no Infinity (the page's ∞ glyph).

        Budget: ~1.25s per request at a realistic 6k-row book (~25 clustered
        bootstraps across the five breakdown families plus the per-arm paired
        deltas; measured 0.6s median on the dev box -- the budget leaves headroom
        for slower boxes and Azure round-trips). Combined with /api/forward-books
        the 60s poll cycle carries ~2.4s (~4% duty); revisit (cache across requests)
        if wall time approaches the poll interval or a second polling client appears.
        """
        trades = facet_filter(load_research_paper_trades(session), facet)
        if play_type != "all":
            trades = [t for t in trades if t.play_type == play_type]

        # (2) Variant leaderboard: judged on the BASELINE exit arm, window cut applied
        # AFTER the arm filter. The `t.opened_date and ...` truthiness is deliberate
        # page parity: an undated row drops from windowed views, stays in "all".
        baseline = [t for t in trades if t.arm == BASELINE]
        if window != "all":
            cutoff = date.today() - timedelta(days=int(window))
            baseline = [t for t in baseline if t.opened_date and t.opened_date >= cutoff]
        by_variant = breakdown(baseline, "variant")
        leaderboard = [
            _leaderboard_row(
                v, by_variant[v], [t for t in baseline if str(t.variant) == v], facet
            )
            for v in leaderboard_order(by_variant)
        ]

        # (3) Everything downstream is ONE screen variant: the arms share fills only
        # within a single screen config, so a second variant would pollute the A/B.
        scoped = [t for t in trades if t.variant == DEFAULT_VARIANT]
        by_arm = breakdown(scoped, "arm")
        arm_names = sorted(by_arm)
        arms = [_arm_row(a, by_arm[a], scoped, facet) for a in arm_names]

        # KPI/breakdown subset: the page's arm-detail radio pinned at its default --
        # the BASELINE arm when several arms exist (else the first alphabetically;
        # with a single arm the book passes through unchanged, exactly like the page).
        if len(arm_names) > 1:
            detail = BASELINE if BASELINE in arm_names else arm_names[0]
            detail_trades = [t for t in scoped if str(t.arm) == detail]
        else:
            detail_trades = scoped

        summary = summarize(detail_trades)
        pf = summary.profit_factor
        kpis: dict[str, object] = {
            "expectancy": _stat_dict(summary, detail_trades, facet),
            "win_rate": summary.win_rate,
            "fill_rate": summary.fill_rate,
            "profit_factor": None if pf == float("inf") else pf,
            "n_closed": summary.n_closed,
        }

        by_tf = breakdown(detail_trades, "timeframe")
        score_rows: list[dict[str, object]] = []
        for label, group in _bucket_trades_by_score(
            score_stamped(detail_trades), SCORE_EDGES
        ).items():
            band = summarize(group)
            if band.n_closed > 0:  # an empty band would read as a spurious zero
                score_rows.append(_breakdown_row(label, band, group, facet))
        trend_pool = [t for t in detail_trades if t.market_trend is not None]
        vol_pool = [t for t in detail_trades if t.market_vol is not None]
        by_trend = breakdown(trend_pool, "market_trend")
        by_vol = breakdown(vol_pool, "market_vol")
        breakdowns: dict[str, object] = {
            "timeframe": [
                _breakdown_row(
                    k, by_tf[k],
                    [t for t in detail_trades if str(t.timeframe) == k], facet,
                )
                for k in sorted(by_tf)
            ],
            "rank": [
                _breakdown_row(label, summarize(group), group, facet)
                for label, group in _bucket_trades_by_rank(
                    detail_trades, _RANK_EDGES
                ).items()
            ],
            "score": score_rows,
            "market_trend": [
                _breakdown_row(
                    k, by_trend[k],
                    [t for t in trend_pool if str(t.market_trend) == k], facet,
                )
                for k in sorted(by_trend)
            ],
            "market_vol": [
                _breakdown_row(
                    k, by_vol[k],
                    [t for t in vol_pool if str(t.market_vol) == k], facet,
                )
                for k in sorted(by_vol)
            ],
        }

        return {
            "kpis": kpis,
            "leaderboard": leaderboard,
            "arms": arms,
            "breakdowns": breakdowns,
            "equity_curve": [[d.isoformat(), r] for d, r in equity_curve(detail_trades)],
        }

    @app.get("/api/gate")
    def gate(session: Session = Depends(_session)) -> dict[str, object]:
        """The advisory autonomy gate + today's analyst spend, as one status object.

        ``ready`` and ``countdown`` come from ``pipeline.autonomy`` VERBATIM -- the
        countdown's line format is pinned by tests/pipeline/test_autonomy_countdown.py,
        so the arithmetic is never reimplemented here. A missing verdicts sidecar
        reads as not-ready (the gate's own missing-file posture), never an error.
        ``execution_mode`` reads the env-backed settings at request time;
        ``analyst_spend_today_usd`` sums ``est_cost_usd`` over TODAY's AnalystCall
        rows (NULL costs -- the deterministic/fallback path -- count 0.0).
        """
        report = autonomy_gate(session, edge_dir=resolve_edge_dir(edge_dir))
        calls = session.scalars(
            select(AnalystCall).where(AnalystCall.created_date == date.today())
        )
        return {
            "ready": report.ready,
            "countdown": gate_countdown(report),
            "execution_mode": load_settings().execution_mode,
            "analyst_spend_today_usd": sum((c.est_cost_usd or 0.0 for c in calls), 0.0),
        }

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

    @app.get("/api/events")
    async def events() -> EventSourceResponse:
        """The SSE wake channel: a ``change`` event whenever the change token moves
        -- plus ALWAYS one on (re)connect, since ``last`` starts None. Consumers
        (Task 9's useEventWake) must treat an event as a refetch trigger, never as
        evidence something changed.

        Data changes come from EXTERNAL processes (scheduled jobs, git pulls), so
        server-side polling is the only correct driver -- there is no in-process
        write to hook. The endpoint is async so the infinite generator never pins a
        threadpool worker; each token read hops through ``anyio.to_thread`` (the DB
        probe is sync) and no Session survives across the sleeps. The frontend's 60s
        poll stays the floor -- this channel only wakes it early: the poll is the
        defense against dropped SSE connections and change classes the token doesn't
        watch. GET under /api (covered by the dev vite proxy); no X-Cockpit header:
        it mutates nothing.
        """

        async def stream() -> AsyncIterator[dict[str, str]]:
            last: dict[str, str] | None = None
            while True:
                token = await anyio.to_thread.run_sync(
                    _safe_change_token, _engine, edge_dir
                )
                if token != last:
                    last = token
                    yield {"event": "change", "data": json.dumps(token)}
                await anyio.sleep(_WAKE_POLL_S)

        return EventSourceResponse(stream(), ping=_WAKE_POLL_S)

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
    snapshot refresh; they are cheap env reads). No broker configured -> None,
    which ``BrokerSnapshot`` caches as a None snapshot for its TTL."""
    return build_broker(load_settings())


# One cadence for the wake channel: the token poll AND sse-starlette's keepalive
# ping tick together, deliberately -- a ping without a fresh token read (or vice
# versa) buys nothing, so the two must not drift apart.
_WAKE_POLL_S = 15


def _watermark(value: object | None) -> str:
    """One watermark's wire form: ISO for dates/datetimes, ``str()`` for ids, the
    literal ``"none"`` for an empty table -- strings only, so token equality is a
    plain dict compare and ``json.dumps`` never meets a date object."""
    if value is None:
        return "none"
    if isinstance(value, date):  # datetime is a date subclass; isoformat covers both
        return value.isoformat()
    return str(value)


def _change_token(engine: Engine, edge_dir: Path) -> dict[str, str]:
    """The wake channel's change token: cheap max-watermarks over every store the
    cockpit renders -- screen run, paper-trade write, trade close, sent email,
    market report, funnel snapshot, newest reflection verdicts file. Equality means
    "nothing worth refetching"; the values are opaque to the frontend. The ``exit``
    watermark exists because a close is an UPDATE on paper_trades (no new id, no
    updated_at column) -- invisible to ``max(PaperTrade.id)`` -- but every close
    path (shadow.py, reconcile.py, exitcheck.py) INSERTS an ExitEvent. One
    short-lived Session per call, never held across the stream loop's sleeps.
    ``edge_dir`` is the RESOLVED edge directory (the caller threads
    ``resolve_edge_dir`` -- same seam as the heartbeats)."""
    with Session(engine) as session:
        funnel = latest_reversal_funnel(session)
        token = {
            "signal": _watermark(session.scalar(select(func.max(Signal.run_date)))),
            "trade": _watermark(session.scalar(select(func.max(PaperTrade.id)))),
            "exit": _watermark(session.scalar(select(func.max(ExitEvent.id)))),
            "email": _watermark(session.scalar(select(func.max(EmailLog.sent_at)))),
            "weather": _watermark(
                session.scalar(select(func.max(MarketReport.run_date)))
            ),
            "funnel": _watermark(funnel.run_date if funnel is not None else None),
        }
    token["verdicts"] = _watermark(newest_verdicts_mtime(edge_dir))
    return token


def _safe_change_token(
    engine_factory: Callable[[], Engine], edge_dir: Path | None
) -> dict[str, str]:
    """``_change_token`` with the down-DB posture: ANY failure (engine creation,
    query, filesystem) collapses to the sentinel ``{"db": "down"}`` -- the stream
    loop must never die, and recovery reads as a change (the sentinel can never
    equal a real token). The factory is the app's cached ``_engine`` accessor, so
    the down case is re-probed per tick, never latched."""
    try:
        return _change_token(engine_factory(), resolve_edge_dir(edge_dir))
    except Exception:
        return {"db": "down"}


def _stat_dict(
    summary: PerformanceSummary, subset: list[PaperTrade], facet: str
) -> dict[str, object]:
    """The one Stat-building call every aggregate row shares: the summary's expectancy
    wrapped with the SUBSET's provable cost level (the cutoff rule in
    ``cost_level_for`` -- ``subset`` must be exactly the rows ``summary`` was computed
    from), an explicit null ``corpus_id`` (not persisted yet), and the facet the row
    was computed under."""
    return stat_from_summary(
        summary, cost_level=cost_level_for(subset), corpus_id=None, facet=facet
    ).as_dict()


def _cohort(
    key: str,
    strength: str | None,
    summary: PerformanceSummary,
    trades: list[PaperTrade],
    facet: str,
) -> dict[str, object]:
    """One cohort row -- see ``_stat_dict`` for the Stat posture."""
    return {"key": key, "strength": strength, "stat": _stat_dict(summary, trades, facet)}


# The Streamlit performance page's fixed rank-bucket edges (1-5 / 6-10 / 11+),
# ported as-is -- they are the parity contract. The score-band edges are the shared
# SCORE_EDGES constant (analytics.performance): the reflection's verdict buckets and
# this breakdown must band identically.
_RANK_EDGES: tuple[int, ...] = (5, 10)


def _leaderboard_row(
    variant: str, summary: PerformanceSummary, subset: list[PaperTrade], facet: str
) -> dict[str, object]:
    """One strategy-leaderboard row. The Stat guards the EDGE claim (expectancy + CI);
    win/fill rates and the signal count ride as plain context fields -- same posture
    as the cohort rows' ``key``/``strength``. ``fill_rate``/``n_total`` are the fill
    visibility rule (2026-07-01 audit): a variant that 'wins' by rarely filling must
    show it where the ranking is read. ``flag`` is ``leaderboard_flag``'s shared
    trust label (iid beats thin/ok: an unclustered bound is the first thing to see)."""
    return {
        "variant": variant,
        "stat": _stat_dict(summary, subset, facet),
        "win_rate": summary.win_rate,
        "fill_rate": summary.fill_rate,
        "n_total": summary.n_total,
        "flag": leaderboard_flag(summary),
    }


def _arm_row(
    arm: str, summary: PerformanceSummary, pooled: list[PaperTrade], facet: str
) -> dict[str, object]:
    """One experiment-arm row. ``pooled`` is the whole DEFAULT_VARIANT book (every
    arm): the paired delta needs both legs of each fill. The delta Stat is built by
    ``stat_from_paired_delta`` -- the one home for the mapping, shared with
    settlement.py's arm branch -- with the cost level stamped over the POOLED book
    (both delta sides). The baseline row carries ``delta: null`` (there is no
    self-delta) with ``n_pairs: 0`` -- no pairs back a delta claim there."""
    subset = [t for t in pooled if str(t.arm) == arm]
    row: dict[str, object] = {"arm": arm, "stat": _stat_dict(summary, subset, facet)}
    if arm == BASELINE:
        row["n_pairs"] = 0
        row["delta"] = None
        return row
    pad = paired_arm_delta(pooled, arm=arm)
    row["n_pairs"] = pad.n_pairs
    row["delta"] = stat_from_paired_delta(
        pad, cost_level=cost_level_for(pooled), facet=facet
    ).as_dict()
    return row


def _breakdown_row(
    key: str, summary: PerformanceSummary, subset: list[PaperTrade], facet: str
) -> dict[str, object]:
    """One breakdown row (timeframe / rank / score / regime): the Stat guards the
    expectancy claim; win rate and the closed count ride as plain context fields."""
    return {
        "key": key,
        "stat": _stat_dict(summary, subset, facet),
        "win_rate": summary.win_rate,
        "n_closed": summary.n_closed,
    }


# Forward Books wall order: decision-forcing cards first (settled and futile both
# await a human), then still-accruing books, then retired history. Built FROM
# settlement's STATES tuple so the two modules cannot drift: a fifth state breaks
# this unpacking at import time -- loudly, in tests -- never as a request-time 500.
_RETIRED, _FUTILE, _SETTLED, _ACCRUING = STATES
_STATE_RANK = {_SETTLED: 0, _FUTILE: 0, _ACCRUING: 1, _RETIRED: 2}


def _card_dict(c: SettlementCard) -> dict[str, object]:
    """Explicit wire form for a settlement card -- hand-rolled like ``_beat_dict``
    (never ``dataclasses.asdict`` on the wire): Stats serialize via their own
    ``as_dict`` and the spark's (date, value) tuples become JSON pairs here, visibly."""
    return {
        "name": c.name,
        "kind": c.kind,
        "play_type": c.play_type,
        "state": c.state,
        "n_accrued": c.n_accrued,
        "n_needed": c.n_needed,
        "eta": c.eta,
        "stopping_rule": c.stopping_rule,
        "registered_sha": c.registered_sha,
        "registered_at": c.registered_at,
        "mde_r": c.mde_r,
        "book": c.book.as_dict(),
        "control": c.control.as_dict(),
        "delta": c.delta.as_dict(),
        "upper_bound_type": c.upper_bound_type,
        "spark": [[d, v] for d, v in c.spark],
        "decision": c.decision,
    }


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

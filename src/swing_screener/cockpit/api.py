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
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, cast

import anyio.to_thread
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
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
from swing_screener.analytics.pl import PositionPL, position_pl
from swing_screener.cockpit.gh import latest_workflow_run
from swing_screener.cockpit.heartbeats import (
    Heartbeat,
    collect_heartbeats,
    newest_verdicts_mtime,
)
from swing_screener.cockpit.livedata import BrokerSnapshot, QuoteCache, Snapshot
from swing_screener.cockpit.settlement import (
    STATES,
    SettlementCard,
    build_cards,
    facet_filter,
)
from swing_screener.cockpit.stats import stat_from_paired_delta, stat_from_summary
from swing_screener.config import StrategyConfig
from swing_screener.data import quotes
from swing_screener.db.models import (
    AnalystCall,
    EmailLog,
    ExecutionLog,
    ExitEvent,
    MarketReport,
    PaperTrade,
    Signal,
    Trade,
)
from swing_screener.db.repo import (
    AlreadyClosedError,
    add_trade,
    close_trade_with_event,
    count_open_positions,
    create_analysis_request,
    execution_logs_for_day,
    get_analysis_request,
    get_closed_trades,
    get_open_trades,
    latest_reversal_funnel,
    latest_run_date,
    list_analysis_requests,
    load_closed_paper_trades,
    load_open_live_trades,
    load_research_paper_trades,
    realized_r_on,
)
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.autonomy import autonomy_gate, gate_countdown
from swing_screener.pipeline.broker import BrokerClient
from swing_screener.pipeline.broker_alpaca import build_broker
from swing_screener.pipeline.disarm import _STOP_TYPES
from swing_screener.pipeline.execution import (
    LIVE_ACCOUNT,
    MANUAL_ACCOUNT,
    OFF_ACCOUNT,
    PAPER_ACCOUNT,
)
from swing_screener.pipeline.insight import size_order
from swing_screener.pipeline.proposed import (
    ProposedVariant,
    decide_proposal,
    load_proposed_for,
    to_config,
)
from swing_screener.pipeline.registry import load_experiments
from swing_screener.pipeline.variants import DEFAULT_VARIANT
from swing_screener.settings import (
    load_settings,
    resolve_edge_dir,
    resolve_execution,
    resolve_risk_unit,
)
from swing_screener.signals.actionability import classify
from swing_screener.storage.blob import resolve_chart_bytes, resolve_pdf_bytes


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


class TradeCreate(BaseModel):
    """POST /api/trades body. The validation LIVES HERE -- ``repo.add_trade`` is a
    pure persist that checks nothing -- so every rule the retired Streamlit entry
    form enforced is a model rule: ticker required (stripped + uppercased), entry
    and size positive, stop below entry, target above entry (long-only book).
    ``max_length`` bounds mirror the Trade columns so Azure SQL never truncates.
    Template rule for every action model: floats pin ``allow_inf_nan=False`` --
    hand-crafted JSON ``Infinity``/``NaN`` sails through ``gt`` and comparison
    validators (NaN compares False against everything) and would poison R math."""

    ticker: str = Field(max_length=16)
    timeframe: str = Field(default="1d", max_length=32)
    horizon: str = Field(default="medium", max_length=32)
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    size: float = Field(gt=0, allow_inf_nan=False)
    stop: float = Field(allow_inf_nan=False)
    target: float = Field(allow_inf_nan=False)
    notes: str = Field(default="", max_length=256)
    signal_id: int | None = None

    @field_validator("ticker")
    @classmethod
    def _ticker_required_upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("ticker is required")
        return v

    @model_validator(mode="after")
    def _long_geometry(self) -> "TradeCreate":
        if self.stop >= self.entry_price:
            raise ValueError("stop must be below the entry price (long)")
        if self.target <= self.entry_price:
            raise ValueError("target must be above the entry price (long)")
        return self


class TradeClose(BaseModel):
    """POST /api/trades/{id}/close body. ``exit_date`` defaults to today at the
    endpoint (a request body should not bake in the server's clock) and is
    range-checked there too -- the bounds need the trade row. A blank reason
    falls back to ``manual`` -- the Streamlit close form's behavior."""

    exit_price: float = Field(gt=0, allow_inf_nan=False)
    exit_date: date | None = None
    exit_reason: str = Field(default="manual", max_length=32)

    @field_validator("exit_reason")
    @classmethod
    def _reason_or_manual(cls, v: str) -> str:
        return v.strip() or "manual"


class AnalysisCreate(BaseModel):
    """POST /api/analysis body: one ticker, required, stripped + uppercased, and
    ASCII-only -- tickers are ASCII by construction, and a fullwidth look-alike
    (ＡＭＤ) would both miss the real symbol at fetch time and be stripped from the
    PDF filename downstream (see ``_pdf_filename``), so it is rejected at the front
    door. ``max_length`` mirrors ``AnalysisRequest.ticker``'s String(16) (the
    template rule); no float fields, so there is no ``allow_inf_nan`` to pin."""

    ticker: str = Field(max_length=16)

    @field_validator("ticker")
    @classmethod
    def _ticker_required_upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("ticker is required")
        if not v.isascii():
            raise ValueError("ticker must be ASCII")
        return v


class ProposalDecision(BaseModel):
    """POST /api/proposals/{play_type}/{name}/approve|withdraw body: the decision
    reason -- required (non-blank after strip), <=200 chars. It lands VERBATIM in
    the store's rationale audit append (`` APPROVED <date>: <reason>``, the 2026-07
    hand-edit convention), so the bound is a sanity cap on an append-forever field,
    not a column width. No float fields, so there is no ``allow_inf_nan`` to pin
    (the template rule)."""

    reason: str = Field(max_length=200)

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("reason is required")
        return v


# Prices within this absolute tolerance count as EQUAL for override stamping: the
# prefill round-trips through JSON floats and a UI number input, so exact equality
# would stamp phantom "moved 0.0%" overrides on faithful fills.
_FAITHFUL_TOL = 0.005


def _pct_part(label: str, actual: float, planned: float) -> str | None:
    """One ``'{label} moved {+x.x%}'`` part, or None when the move isn't real:
    within the absolute tolerance, an unusable denominator, or -- the high-price
    trap -- a move whose FORMATTED percent rounds to ±0.0% (a one-cent nudge of a
    $500 stop clears the absolute tolerance but stamps a zero-looking override)."""
    if planned <= 0 or abs(actual - planned) <= _FAITHFUL_TOL:
        return None
    text = f"{(actual - planned) / planned:+.1%}"
    if text in ("+0.0%", "-0.0%"):
        return None
    return f"{label} moved {text}"


def _override_note(body: TradeCreate, sig: Signal) -> str | None:
    """The honest-flagging stamp: how ``body`` deviates from ``sig``'s plan, or None.

    Entry deviation is expressed in zone-R (risk = entry_ceiling - stop, the unit the
    signal's own R math uses); stop/target moves in percent of the signal's level,
    with zero-LOOKING moves suppressed (see ``_pct_part``). Format (rendered
    VERBATIM by the UI -- see the endpoint docstring):
    ``entry +0.50R above ceiling; stop moved +1.1%; target moved -2.0%``. A
    degenerate zone (ceiling <= stop, no R unit to speak in) skips the entry part
    rather than dividing by zero.
    """
    parts: list[str] = []
    risk = sig.entry_ceiling - sig.stop
    if risk > _FAITHFUL_TOL:
        if body.entry_price > sig.entry_ceiling + _FAITHFUL_TOL:
            over = (body.entry_price - sig.entry_ceiling) / risk
            parts.append(f"entry +{over:.2f}R above ceiling")
        elif body.entry_price < sig.entry_floor - _FAITHFUL_TOL:
            under = (sig.entry_floor - body.entry_price) / risk
            parts.append(f"entry -{under:.2f}R below floor")
    for part in (_pct_part("stop", body.stop, sig.stop),
                 _pct_part("target", body.target, sig.target)):
        if part is not None:
            parts.append(part)
    return "; ".join(parts)[:256] if parts else None  # cap: the column is String(256)


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

    @app.post("/api/azure-login", dependencies=[Depends(_require_cockpit)])
    def azure_login() -> dict[str, object]:
        """Spawn ``az login`` to refresh the AAD credential (design doc 2026-07-06).

        Contract: header-guarded (``_require_cockpit``), Azure-mode only (a login
        cannot fix a sqlite file), single-flight, and NO success signal --
        ``/api/health`` is the sole recovery oracle; the per-connection token fetch
        picks up the new credential on the next poll. Never touches the engine."""
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

    @app.post("/api/trades", dependencies=[Depends(_require_cockpit)])
    def log_trade(
        body: TradeCreate, session: Session = Depends(_session)
    ) -> dict[str, object]:
        """Log a REAL trade Oliver actually took -- records it, places no order.

        Contract: header-guarded (``_require_cockpit``); ``TradeCreate`` is the
        only validation gate (422 with field detail -- the repo persists blindly);
        the server stamps ``entry_date`` = today, never the client. When the body
        carries a ``signal_id``, the Signal row MUST exist -- 422 ``unknown
        signal_id`` otherwise, on every backend identically: the FK is real, so
        sqlite (no FK pragma) would store a dangling id silently while Azure SQL
        would reject the insert with an IntegrityError-shaped 503. With the row,
        the fill is verified against the engine's plan and any deviation is
        stamped into ``override`` in the format the UI renders verbatim:
        ``entry +0.50R above ceiling`` / ``entry -0.25R below floor`` (zone-R:
        risk = entry_ceiling - stop), ``stop moved +1.1%``, ``target moved -2.0%``,
        parts joined by ``'; '``, capped at 256 chars, zero-looking percents
        suppressed. ``override`` is null when the fill is faithful OR unprefilled
        -- the UI's unlinked tag (``signal_id`` null) tells those apart.
        """
        override: str | None = None
        if body.signal_id is not None:
            sig = session.get(Signal, body.signal_id)
            if sig is None:
                raise HTTPException(status_code=422, detail="unknown signal_id")
            override = _override_note(body, sig)
        trade = add_trade(session, Trade(
            ticker=body.ticker, timeframe=body.timeframe, horizon=body.horizon,
            entry_date=date.today(), entry_price=body.entry_price, size=body.size,
            stop=body.stop, target=body.target, notes=body.notes,
            signal_id=body.signal_id, override=override,
        ))
        return {"trade_id": trade.id, "override": trade.override,
                "entry_date": trade.entry_date.isoformat()}

    @app.post("/api/trades/{trade_id}/close", dependencies=[Depends(_require_cockpit)])
    def close_trade_action(
        trade_id: int, body: TradeClose, session: Session = Depends(_session)
    ) -> dict[str, object]:
        """Close a real trade at the price Oliver reports.

        Header-guarded (``_require_cockpit``); 404 on an unknown id, 409 when
        already closed (the repo's ``AlreadyClosedError`` -- re-closing would
        overwrite the recorded exit), 422 when ``exit_date`` predates the trade's
        entry or postdates today (input validation, checked post-fetch because it
        needs the trade row). The close and its ``ExitEvent(reason='manual_close')``
        -- audit trail + change token -- land in ONE transaction
        (``close_trade_with_event``): both rows or neither, so a mid-close failure
        leaves the trade open and a retry succeeds instead of 409ing against a
        half-recorded close. The manual_close reason is EXCLUDED from
        ``pending_exit_alerts``, so the hourly exit job never emails an urgent
        alert about a close performed seconds ago in the cockpit. ``realized_r``
        is ``(exit - entry) / (entry - stop)`` and null when the recorded risk is
        degenerate (stop raised to/above entry, e.g. breakeven management) -- the
        close itself still happens; ``realized_usd`` is ``(exit - entry) * size``
        and always computes.
        """
        pre = session.get(Trade, trade_id)  # for exit_date bounds + the event message
        if pre is None:
            raise HTTPException(status_code=404, detail=f"no trade with id {trade_id}")
        exit_date = body.exit_date if body.exit_date is not None else date.today()
        if exit_date < pre.entry_date:
            raise HTTPException(
                status_code=422, detail="exit_date is before the trade's entry_date")
        if exit_date > date.today():
            raise HTTPException(status_code=422, detail="exit_date is in the future")
        try:
            trade, _event = close_trade_with_event(
                session, trade_id, exit_date=exit_date, exit_price=body.exit_price,
                exit_reason=body.exit_reason, event_reason="manual_close",
                event_message=f"{pre.ticker} closed manually @ {body.exit_price:g}",
                created_date=date.today(),
            )
        except AlreadyClosedError as exc:  # BEFORE ValueError: it subclasses it
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:  # unknown id -- unreachable after the fetch, kept
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        risk = trade.entry_price - trade.stop
        realized_r = (body.exit_price - trade.entry_price) / risk if risk > 0 else None
        return {
            "trade_id": trade.id,
            "realized_r": realized_r,
            "realized_usd": (body.exit_price - trade.entry_price) * trade.size,
            "exit_date": exit_date.isoformat(),
            "exit_reason": trade.exit_reason,
        }

    @app.get("/api/positions")
    def positions(session: Session = Depends(_session)) -> dict[str, object]:
        """The whole trades screen in one read: open positions with per-row P/L,
        the three cap gauges, closed history, and the realized equity curve.

        Nuances, each deliberate:

        * PER-ROW degradation: ONE malformed trade or missing quote must never
          503 the zone. Every open row is KEPT; a row whose ``position_pl``
          raises (non-positive risk, or a degenerate zero price) degrades to
          ``pl: null`` with the badge STILL computed from price/stop/target (a
          breached stop must read red even on a malformed row); a missing quote
          (absent from the cache's dict) nulls ``last_close`` and the badge is
          ``unknown``. ``last_close`` is the close form's prefill source, so it
          stays populated whenever the quote exists -- even on a row whose P/L
          math is broken.
        * ``pl.unrealized_pct`` is a FRACTION on the wire (0.04, not 4.0) --
          the frontend formats percents, mirroring ``analytics.pl``.
        * ``badge``: ``red`` price <= stop, ``yellow`` price >= target,
          ``green`` between, ``unknown`` without a price.
        * LIVE rows (open ``account="live"`` PaperTrades) have NO size column:
          ``size``/dollar P/L come from the spec'd ExecutionLog join -- the
          NEWEST row for the ticker with status ``submitted_live``/``filled_live``
          (``_live_shares``) -- else both stay null with the R-multiple still
          rendered from the persisted per-share ``risk``; the size-independent
          percent fields stay honest either way. A pending-entry live row
          (``entry_price`` null) carries ``pl: null``; its badge still reads off
          the quote (stop/target are always recorded).
        * BRACKET lamp, per kind: no broker snapshot -> ``unknown`` (absence of
          evidence is never a claim); a venue-held protective sell order
          (``order_type`` in disarm's ``_STOP_TYPES``) for the symbol ->
          ``armed``; else ``db-only`` -- the recorded stop exists only as a DB
          number. A REAL/manual row is NEVER ``unprotected``: the venue does not
          know it exists, so ``db-only`` is its honest ceiling. ``unprotected``
          is reserved for a row with no recorded stop at all (both stop columns
          are NOT NULL today, so it is a wire-contract state, not a live one).
        * CAPS mirror ``execution._limit_block``'s reads EXACTLY: ``notional``
          sums ``ExecutionLog.notional`` over ``execution_logs_for_day`` (the
          REPO filters to the counting statuses -- skipped/canceled/rejected
          never reserved notional); ``loss_r`` is ``realized_r_on`` -- an R
          THRESHOLD, today's realized R with sign preserved (the breaker fires
          at ``used <= -limit``), NOT a spent-dollars meter; ``concurrent`` is
          ``count_open_positions`` (PaperTrade rows only -- exactly what the
          adapter checks; open manual Trades don't count there either).
          ``account`` maps from the CURRENT execution mode (off -> the research
          label, per the adapter constants); ``run_date = latest_run_date``;
          with no runs yet the day-scoped used values are an honest 0.0 and
          ``run_date`` null. A ``None`` cap is unbounded: ``limit: null`` on the
          wire, NEVER 0/0.
        * ``closed`` arrives newest-exit first (the repo's order); ``equity`` is
          the retired Streamlit ``_render_closed`` math verbatim: dated closes
          ascending, running sum of ``((exit or entry) - entry) * size`` rounded
          to cents, an undated close listed but never plotted.
        * ONE ``QuoteCache.get`` for all open tickers per request (the TTL cache
          makes a cold fetch at most once per window); ``quotes_as_of``
          timestamps the window's latest contribution, not each price.
          ``broker_as_of`` is null without a snapshot.

        Budget: cheap per request; the outlier is the cold quote fetch --
        multi-second yfinance under the single-flight lock, at most once per
        600s window.
        """
        open_real = get_open_trades(session)
        open_live = load_open_live_trades(session)
        quote_cache: QuoteCache = app.state.quote_cache
        quote_result = quote_cache.get(
            [t.ticker for t in open_real] + [p.ticker for p in open_live])
        prices = quote_result.prices
        broker_snapshot: BrokerSnapshot = app.state.broker_snapshot
        snapshot = broker_snapshot.get()
        armed = _armed_symbols(snapshot)

        open_rows: list[dict[str, object]] = [
            _real_position_row(t, prices.get(t.ticker), snapshot=snapshot, armed=armed)
            for t in open_real
        ]
        open_rows += [
            _live_position_row(p, prices.get(p.ticker),
                               _live_shares(session, p.ticker),
                               snapshot=snapshot, armed=armed)
            for p in open_live
        ]

        mode, limits = resolve_execution(load_settings())
        account = _ACCOUNT_FOR_MODE[mode]  # total: load_settings coerces unknown->off
        run_d = latest_run_date(session)
        if run_d is None:  # no runs yet: no day to sum -- honest zeros, null date
            notional_used, loss_used = 0.0, 0.0
        else:
            notional_used = sum(e.notional for e in execution_logs_for_day(
                session, run_date=run_d, account=account))
            loss_used = realized_r_on(session, run_date=run_d, account=account)

        closed_trades = get_closed_trades(session)
        dated = sorted((t for t in closed_trades if t.exit_date is not None),
                       key=lambda t: cast(date, t.exit_date))
        equity: list[list[object]] = []
        running = 0.0
        for t in dated:
            running += _realized_usd(t)
            equity.append([cast(date, t.exit_date).isoformat(), round(running, 2)])

        return {
            "open": open_rows,
            "caps": {
                "account": account,
                "run_date": run_d.isoformat() if run_d is not None else None,
                "notional": {"used": notional_used,
                             "limit": limits.max_daily_notional},
                "loss_r": {"used": loss_used, "limit": limits.max_daily_loss},
                "concurrent": {
                    "used": count_open_positions(session, account=account),
                    "limit": limits.max_concurrent,
                },
            },
            "closed": [{
                "trade_id": t.id,
                "ticker": t.ticker,
                "entry_date": t.entry_date.isoformat(),
                "exit_date": (t.exit_date.isoformat()
                              if t.exit_date is not None else None),
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "size": t.size,
                "realized_usd": _realized_usd(t),
                "exit_reason": t.exit_reason,
            } for t in closed_trades],
            "equity": equity,
            "quotes_as_of": quote_result.as_of.isoformat(),
            "broker_as_of": (snapshot.as_of.isoformat()
                             if snapshot is not None else None),
        }

    @app.get("/api/trade-defaults")
    def trade_defaults(
        signal_id: int, session: Session = Depends(_session)
    ) -> dict[str, object]:
        """The log-trade form's prefill for one Signal: the engine's levels
        VERBATIM (never recomputed -- the analyst/UI can never move a level),
        live actionability, a suggested entry, and the conviction-'medium' size.

        404 on an unknown ``signal_id`` (missing/garbage is FastAPI's 422).
        ``actionability`` classifies the entry zone at the cached quote
        (``signals.actionability.classify``) and is null WITHOUT a quote -- so is
        ``suggested_entry``, which is the quote CLAMPED into
        ``[entry_floor, entry_ceiling]``: the prefill never chases an extended
        price above the ceiling nor bids below the zone. ``sizing`` is
        ``insight.size_order`` at conviction 'medium' over
        ``resolve_risk_unit(load_settings())``; ``shares == 0`` is the deliberate
        'sizing unconfigured' signal (``unconfigured: true`` -- the UI renders
        R-multiples, never a guessed dollar). One ``QuoteCache.get`` per request.
        """
        sig = session.get(Signal, signal_id)
        if sig is None:
            raise HTTPException(
                status_code=404, detail=f"no signal with id {signal_id}")
        quote_cache: QuoteCache = app.state.quote_cache
        price = quote_cache.get([sig.ticker]).prices.get(sig.ticker)
        actionability: dict[str, object] | None = None
        suggested: float | None = None
        if price is not None:
            result = classify(entry_floor=sig.entry_floor,
                              entry_ceiling=sig.entry_ceiling,
                              stop=sig.stop, price=price)
            actionability = {"status": result.status, "dist_r": result.dist_r}
            suggested = min(max(price, sig.entry_floor), sig.entry_ceiling)
        risk_unit, max_shares = resolve_risk_unit(load_settings())
        shares, risk_dollars = size_order(
            conviction="medium", entry_ceiling=sig.entry_ceiling, stop=sig.stop,
            risk_unit_dollars=risk_unit, max_shares=max_shares)
        return {
            "signal": {
                "ticker": sig.ticker,
                "timeframe": sig.timeframe,
                "horizon": sig.horizon,
                "play_type": sig.play_type,
                "entry_floor": sig.entry_floor,
                "entry_ceiling": sig.entry_ceiling,
                "stop": sig.stop,
                "target": sig.target,
                "conviction_tier": sig.conviction_tier,
            },
            "last_close": price,
            "actionability": actionability,
            "suggested_entry": suggested,
            "sizing": {"shares": shares, "risk_dollars": risk_dollars,
                       "unconfigured": shares == 0},
        }

    @app.post("/api/analysis", dependencies=[Depends(_require_cockpit)])
    def request_analysis(
        body: AnalysisCreate, session: Session = Depends(_session)
    ) -> dict[str, object]:
        """Queue an on-demand deep-analysis run for one ticker.

        Header-guarded (``_require_cockpit``); ``AnalysisCreate`` strips +
        uppercases (422 on empty/overlong). The SERVER stamps ``requested_at =
        datetime.now(UTC)`` -- the worker (``notify.ondemand``) claims and
        requeues by UTC comparison, so the client clock never ages a request.
        Queue-view reorder, disclosed: the retired Streamlit form stamped naive
        LOCAL time, and the list orders by the stored value, so old naive rows
        can sort out of true order against UTC stamps by up to the zone offset
        until they age out -- a one-time cosmetic reorder, not a processing
        change. The response echoes the aware stamp; the DB round-trips it
        tz-naive (UTC clock fields, see ``_stalled``)."""
        stamp = datetime.now(UTC)
        req = create_analysis_request(session, ticker=body.ticker, requested_at=stamp)
        return {"id": req.id, "ticker": req.ticker, "status": req.status,
                "requested_at": stamp.isoformat()}

    @app.get("/api/analysis")
    def analysis_list(
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """The deep-analysis queue, newest requested first, plus who drains it.

        ``stalled`` mirrors the worker's requeue window EXACTLY (running longer
        than ``_STALE_AFTER``): the next worker pass will requeue exactly those
        rows, so the UI can say 'stalled -- will retry' instead of spinning.
        ``worker`` derives from the DB URL (``_worker_label``): 'cloud (*/15min)'
        for Azure, else 'manual' -- the UI copy for manual says requests wait for
        ``python -m swing_screener.notify.ondemand``. Timestamps are served as
        unambiguous UTC ('+00:00'-suffixed, ``_utc_iso``): naive DB values are
        stamped UTC, because JS's ``Date()`` parses naive ISO as LOCAL and would
        skew every relative-time render by the zone offset. started_at and
        finished_at are worker-stamped UTC, so the stamp is unconditionally
        correct; legacy Streamlit ``requested_at`` rows were naive LOCAL and
        wear a bounded display offset until they age out (see the POST
        docstring). ``has_pdf``/``chart_count`` let the UI draw asset
        affordances without touching a resolver. Budget: one LIMITed SELECT,
        no resolver or network calls."""
        now = datetime.now(UTC)
        rows = list_analysis_requests(session, limit=limit)
        return {
            "requests": [{
                "id": r.id,
                "ticker": r.ticker,
                "status": r.status,
                "stalled": _stalled(r.status, r.started_at, now=now),
                "requested_at": _utc_iso(r.requested_at),
                "started_at": _utc_iso(r.started_at),
                "finished_at": _utc_iso(r.finished_at),
                "summary": r.summary,
                "error": r.error,
                "has_pdf": bool(r.pdf_blob_key),
                "chart_count": len(_chart_keys(r.chart_blob_keys)),
            } for r in rows],
            "worker": _worker_label(db_url),
        }

    @app.get("/api/analysis/{request_id}/chart/{index}")
    def analysis_chart(
        request_id: int, index: int, session: Session = Depends(_session)
    ) -> Response:
        """One of a request's chart PNGs, resolved SERVER-SIDE from the stored key.

        SECURITY: both path params are ints; the resolver receives ONLY the
        ``chart_blob_keys`` entry stored on the DB row -- no client-supplied key
        or path ever reaches a resolver (the arbitrary-read hole this closes).
        404 on an unknown id, an out-of-range index (negative included -- never
        end-relative), or an unresolvable key (aged-out blob / missing local
        file). Assets age out of the store, so a 404 here is a normal state."""
        req = get_analysis_request(session, request_id)
        if req is None:
            raise HTTPException(status_code=404, detail="unknown analysis request")
        keys = _chart_keys(req.chart_blob_keys)
        if not 0 <= index < len(keys):
            raise HTTPException(status_code=404, detail="no such chart")
        data = resolve_chart_bytes(keys[index])
        if data is None:
            raise HTTPException(status_code=404, detail="chart unavailable")
        return Response(content=data, media_type="image/png")

    @app.get("/api/analysis/{request_id}/pdf")
    def analysis_pdf(
        request_id: int, session: Session = Depends(_session)
    ) -> Response:
        """A request's report PDF, resolved SERVER-SIDE from the stored blob key.

        SECURITY: same posture as the chart proxy -- the resolver receives ONLY
        the row's ``pdf_blob_key``, never anything client-supplied. Served as an
        attachment named ``{ticker}_report.pdf`` (header-sanitized, see
        ``_pdf_filename``). 404 on an unknown id, a row with no PDF (queued /
        failed), or an unresolvable key -- aged-out assets are normal, not
        errors."""
        req = get_analysis_request(session, request_id)
        if req is None:
            raise HTTPException(status_code=404, detail="unknown analysis request")
        data = resolve_pdf_bytes(req.pdf_blob_key)
        if data is None:
            raise HTTPException(status_code=404, detail="pdf unavailable")
        disposition = f'attachment; filename="{_pdf_filename(req.ticker)}"'
        return Response(content=data, media_type="application/pdf",
                        headers={"Content-Disposition": disposition})

    @app.get("/api/signals/{signal_id}/chart")
    def signal_chart(
        signal_id: int, session: Session = Depends(_session)
    ) -> Response:
        """A signal's chart PNG, resolved SERVER-SIDE from ``Signal.chart_path``.

        SECURITY: the id is an int; the resolver receives ONLY the stored
        ``chart_path`` (blob key or local path) -- never a client value. 404 on
        an unknown id, a chartless signal (MOST signals -- the pipeline charts
        only surfaced picks, so 404 is the NORMAL case), or an unresolvable
        path."""
        sig = session.get(Signal, signal_id)
        if sig is None or sig.chart_path is None:
            raise HTTPException(status_code=404, detail="no chart for this signal")
        data = resolve_chart_bytes(sig.chart_path)
        if data is None:
            raise HTTPException(status_code=404, detail="chart unavailable")
        return Response(content=data, media_type="image/png")

    @app.get("/api/proposals")
    def proposals() -> dict[str, object]:
        """The analyst's proposed screen variants, both play types, decision-ready.

        Continuation rows first, then reversal, FILE order within each (the draft
        merge appends, so file order is drafting order). Each row is the stored
        ``ProposedVariant``'s 8 fields verbatim plus three derived decision aids --
        ``gate_verdict``, ``delta_vs_incumbent``, ``noop`` (semantics + leak posture
        in ``_proposal_row``) -- computed against the incumbent ``StrategyConfig()``,
        the same base the optimizer sweeps. A missing proposed.json is a play type
        with nothing queued: its rows are simply absent, never an error. Filesystem
        only -- no DB session, so a down database never blanks this screen."""
        edir = resolve_edge_dir(edge_dir)
        base = StrategyConfig()
        return {"proposals": [
            _proposal_row(pv, base)
            for pt in ("continuation", "reversal")
            for pv in load_proposed_for(pt, edir)
        ]}

    def _decide(
        play_type: str, name: str, *,
        decision: Literal["approved", "withdrawn"], reason: str,
    ) -> ProposedVariant:
        """Shared decision plumbing: ``decide_proposal`` owns the state machine;
        this maps its errors onto the wire. KeyError (unknown name) -> 404 with the
        exception's own message (``args[0]``, never ``str(exc)`` -- str(KeyError)
        wraps the message in quotes); ValueError (refused transition) -> 409 naming
        the current status. Both texts are our own store prose -- no paths, no
        client input. The server stamps ``today``; the client's clock never dates
        an audit append."""
        try:
            return decide_proposal(
                resolve_edge_dir(edge_dir), play_type, name,
                decision=decision, reason=reason,
                today=date.today().isoformat(),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=exc.args[0]) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/proposals/{play_type}/{name}/approve",
              dependencies=[Depends(_require_cockpit)])
    def approve_proposal(
        play_type: Literal["continuation", "reversal"],
        name: str,
        body: ProposalDecision,
    ) -> dict[str, object]:
        """Approve one QUEUED proposal -- approve MARKS, never promotes.

        Header-guarded (``_require_cockpit``); the ``reason`` lands verbatim in the
        rationale audit append. The ONLY write is the store flip ``decide_proposal``
        makes -- an uncommitted working-tree edit (the response's ``note`` says so);
        nothing here touches variants.py or experiments.json. Promotion stays the
        three human edits in the response's ``checklist`` (roster line, registry
        row, this flip), landed as ONE commit -- North Star #1: evidence gates
        promotion, and a tool never promotes. 404/409 mapping in ``_decide``."""
        pv = _decide(play_type, name, decision="approved", reason=body.reason)
        out = _decision_dict(pv, play_type)
        out["checklist"] = _promotion_checklist(play_type)
        return out

    @app.post("/api/proposals/{play_type}/{name}/withdraw",
              dependencies=[Depends(_require_cockpit)])
    def withdraw_proposal(
        play_type: Literal["continuation", "reversal"],
        name: str,
        body: ProposalDecision,
    ) -> dict[str, object]:
        """Withdraw one proposal -- legal from QUEUED and from APPROVED (an
        approval can be walked back; a withdrawal is final until a human
        hand-edits the store). Same guard, audit append, working-tree honesty and
        404/409 mapping as approve; no checklist -- there is nothing to promote."""
        pv = _decide(play_type, name, decision="withdrawn", reason=body.reason)
        return _decision_dict(pv, play_type)

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
    snapshot refresh; they are cheap env reads) BY CHOICE, not accident: broker /
    credential state follows the entrypoints' resolve-at-use pattern, unlike
    ``_default_quote_fetch``'s cache_dir, which is process-stable and bound once.
    No broker configured -> None, which ``BrokerSnapshot`` caches as a None
    snapshot for its TTL."""
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


def _proposal_row(pv: ProposedVariant, base: StrategyConfig) -> dict[str, object]:
    """One proposal's wire form -- hand-rolled like ``_beat_dict``, never ``asdict``.

    ``gate_verdict`` runs the REAL gatekeeper (``proposed.to_config``) inline: 'ok',
    or the ValueError text VERBATIM -- safe by construction, it is config-knob prose
    from our own code (no paths, no client input), the same verdict the optimizer
    logs when it skips the row. ``noop`` mirrors ``optimize.build_config_grid``'s
    no-op guard EXACTLY -- its ``cfg == base`` check on ``to_config``'s output (a
    validated delta equal to the incumbent can never beat it) -- so a row that FAILS
    the gate is invalid, not a no-op: ``noop`` stays False and the verdict says why.
    ``delta_vs_incumbent`` reads ``current`` off the incumbent config only for REAL
    dataclass fields; an unknown knob's current is null -- never a blind ``getattr``,
    which would hand a method repr to a delta key that happened to name one."""
    verdict = "ok"
    noop = False
    try:
        noop = to_config(pv, base) == base
    except ValueError as exc:
        verdict = str(exc)
    known = {f.name for f in fields(base)}
    return {
        "name": pv.name,
        "play_type": pv.play_type,
        "delta": dict(pv.delta),
        "rationale": pv.rationale,
        "hunch_ref": pv.hunch_ref,
        "status": pv.status,
        "drafted_at": pv.drafted_at,
        "provenance": pv.provenance,
        "gate_verdict": verdict,
        "delta_vs_incumbent": [
            {"knob": k, "current": getattr(base, k) if k in known else None,
             "proposed": v}
            for k, v in pv.delta.items()
        ],
        "noop": noop,
    }


def _decision_dict(pv: ProposedVariant, play_type: str) -> dict[str, object]:
    """The shared approve/withdraw wire form: the row's new state plus WHERE the
    write landed. ``play_type``/``file`` come from the URL path param -- the value
    that actually keyed ``decide_proposal``'s store write -- NOT the row's stored
    field, so a hand-mangled row whose ``play_type`` disagrees with the file it
    lives in can never mislabel the file touched. ``file`` is the store's
    REPO-RELATIVE label, never the resolved edge dir (same leak posture as
    ``connection_label``: paths stay off the wire); the ``note`` is the honesty
    line -- ``decide_proposal`` edits the working tree only, and git capturing the
    flip is the human's move."""
    return {
        "name": pv.name,
        "play_type": play_type,
        "status": pv.status,
        "file": f"edge/{play_type}.proposed.json",
        "note": "uncommitted working-tree edit — commit with your decision",
    }


def _promotion_checklist(play_type: str) -> list[str]:
    """The three-artifact promotion checklist, verbatim (Phase 3 plan, Task 8): an
    approval marks ONE row; promotion is these three coupled edits, all human, one
    commit -- the registry charter's shape (roster line so the optimizer sweeps it,
    registry row so settlement can grade it, the store flip this endpoint already
    made)."""
    return [
        "1. pipeline/variants.py — add the roster line (replace(base, **delta))",
        "2. edge/experiments.json — add the registry row (stopping rule, mde_r, "
        "target_ci_halfwidth_r, registered sha)",
        f"3. edge/{play_type}.proposed.json — this flip (done)",
    ]


# Execution mode -> the account its adapter books under (execution.py's constants;
# "off" books nothing and carries the research label purely as a label). Total over
# the mode enum: load_settings coerces any unknown mode to "off" before it gets here.
_ACCOUNT_FOR_MODE = {
    "manual": MANUAL_ACCOUNT,
    "paper": PAPER_ACCOUNT,
    "live": LIVE_ACCOUNT,
    "off": OFF_ACCOUNT,
}


def _badge(price: float, *, stop: float, target: float) -> str:
    """The attention lamp for a priced open row: ``red`` at/under the stop (the
    exit case), ``yellow`` at/over the target (the take-profit case), ``green``
    between. The no-price/unusable-geometry ``unknown`` is the CALLER's branch --
    this helper only speaks when there is a price to compare."""
    if price <= stop:
        return "red"
    if price >= target:
        return "yellow"
    return "green"


def _pl_dict(pl: PositionPL) -> dict[str, object]:
    """``PositionPL``'s wire form, hand-rolled like ``_beat_dict`` (never
    ``dataclasses.asdict`` on the wire). ``unrealized_pct`` is a FRACTION."""
    return {
        "unrealized_pl": pl.unrealized_pl,
        "unrealized_pct": pl.unrealized_pct,
        "r_multiple": pl.r_multiple,
        "dist_to_stop_pct": pl.dist_to_stop_pct,
        "dist_to_target_pct": pl.dist_to_target_pct,
    }


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


def _real_position_row(t: Trade, price: float | None, *,
                       snapshot: Snapshot | None,
                       armed: frozenset[str]) -> dict[str, object]:
    """One REAL (manual) open-trade row. The per-row guard: ``position_pl`` raises
    ``ValueError`` on non-positive risk and a zero price would ZeroDivision the
    distance math -- either degrades THIS row to ``pl: null`` and the row is KEPT
    (one malformed trade never 503s the zone). The badge is computed BEFORE that
    try: it reads only price/stop/target, so a legacy malformed-entry row whose
    price breached the stop still shows RED, never 'unknown' -- that lamp's job is
    'get out'. ``last_close`` likewise stays whatever the quote said: the close
    form prefills from it even when the P/L math is broken."""
    pl: dict[str, object] | None = None
    badge = "unknown"
    if price is not None:
        badge = _badge(price, stop=t.stop, target=t.target)
        try:
            pl = _pl_dict(position_pl(entry=t.entry_price, stop=t.stop,
                                      target=t.target, size=t.size,
                                      current_price=price))
        except (ValueError, ZeroDivisionError):
            pl = None  # the P/L math is untrustable; the badge above still stands
    return {
        "kind": "real",
        "trade_id": t.id,
        "ticker": t.ticker,
        "timeframe": t.timeframe,
        "entry_price": t.entry_price,
        "size": t.size,
        "stop": t.stop,
        "target": t.target,
        "last_close": price,
        "pl": pl,
        "badge": badge,
        "bracket": _bracket(t.ticker, t.stop, snapshot=snapshot, armed=armed),
        "override": t.override,
        "signal_id": t.signal_id,
        "unlinked": t.signal_id is None,
    }


def _live_shares(session: Session, ticker: str) -> int | None:
    """The NEWEST live BUY ticket's share count for ``ticker``, or None -- the spec'd
    join for a live row's missing size column. Only ``submitted_live`` /
    ``filled_live`` rows count (the statuses that created venue exposure --
    canceled/rejected tickets never did), and only ``side == "buy"`` (a sell-side
    live ticket is an EXIT; its shares must never masquerade as position size);
    newest-by-id mirrors ``repo.latest_recorded_stop``'s ordering AND filters.
    One query per open live row; batch (windowed IN) if the live book grows."""
    stmt = (
        select(ExecutionLog.shares)
        .where(
            ExecutionLog.ticker == ticker,
            ExecutionLog.side == "buy",
            ExecutionLog.status.in_(("submitted_live", "filled_live")),
        )
        .order_by(ExecutionLog.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _live_position_row(p: PaperTrade, price: float | None, shares: int | None, *,
                       snapshot: Snapshot | None,
                       armed: frozenset[str]) -> dict[str, object]:
    """One LIVE (broker-owned) open-position row. ``size`` is the ExecutionLog
    join's shares (``_live_shares``) or null; without it the dollar P/L is null
    while the R-multiple still renders from the persisted per-share ``risk`` and
    the size-independent percent fields stay honest. A pending entry
    (``entry_price`` null) carries ``pl: null``; the badge still reads off the
    quote (stop/target are always recorded). Each denominator is guarded per
    FIELD (risk/entry/price non-positive -> that field null) -- same never-503
    posture as the real rows. Keep the field math in lockstep with
    ``analytics.pl.PositionPL`` (the real rows' source, via ``_pl_dict``). Live
    rows have no ``override`` column: null, with ``unlinked`` still keyed off
    ``signal_id`` (a reconciler-materialized fill carries none)."""
    pl: dict[str, object] | None = None
    badge = "unknown"
    if price is not None:
        badge = _badge(price, stop=p.stop, target=p.target)
        if p.entry_price is not None:
            entry = p.entry_price
            pl = {
                "unrealized_pl": ((price - entry) * shares
                                  if shares is not None else None),
                "unrealized_pct": (price - entry) / entry if entry > 0 else None,
                "r_multiple": (price - entry) / p.risk if p.risk > 0 else None,
                "dist_to_stop_pct": (price - p.stop) / price if price > 0 else None,
                "dist_to_target_pct": ((p.target - price) / price
                                       if price > 0 else None),
            }
    return {
        "kind": "live",
        "paper_id": p.id,
        "ticker": p.ticker,
        "timeframe": p.timeframe,
        "entry_price": p.entry_price,
        "size": float(shares) if shares is not None else None,
        "stop": p.stop,
        "target": p.target,
        "last_close": price,
        "pl": pl,
        "badge": badge,
        "bracket": _bracket(p.ticker, p.stop, snapshot=snapshot, armed=armed),
        "override": None,
        "signal_id": p.signal_id,
        "unlinked": p.signal_id is None,
    }


def _realized_usd(t: Trade) -> float:
    """The retired Streamlit ``_render_closed`` realized math, verbatim: an
    exit-less close falls back to the entry price (realized 0.0), never a guess."""
    exit_price = t.exit_price if t.exit_price is not None else t.entry_price
    return (exit_price - t.entry_price) * t.size


# The on-demand worker's requeue window, RESTATED from ``notify.ondemand._STALE_AFTER``
# rather than imported: ondemand's module scope drags the pipeline + notify graph
# (pipeline.run, notify.analysis/pdf/transport) into the cockpit's import graph. A
# lockstep test (tests/cockpit/test_api.py) imports the real constant and pins equality.
_STALE_AFTER = timedelta(minutes=30)


def _stalled(status: str, started_at: datetime | None, *, now: datetime) -> bool:
    """True when a 'running' row has exceeded the worker's requeue window: the next
    worker pass will flip exactly these rows back to 'queued'
    (``repo.requeue_stale_running`` at ``now - _STALE_AFTER``), so the UI says
    'stalled -- will retry' instead of spinning forever. Stored datetimes come back
    NAIVE (sqlite/mssql DATETIME drop tzinfo); the worker stamps UTC, so naive is
    read as UTC."""
    if status != "running" or started_at is None:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return (now - started_at) > _STALE_AFTER


def _utc_iso(value: datetime | None) -> str | None:
    """A stored datetime as an unambiguous UTC wire string ('+00:00'-suffixed).

    DB datetimes come back tz-naive (see ``_stalled``); served naive, JS's
    ``Date()`` would parse them as LOCAL time and skew every relative-time render
    by the zone offset. The worker/cockpit stamp UTC, so stamping UTC here is
    correct; legacy Streamlit ``requested_at`` rows were naive LOCAL and wear a
    bounded display offset until they age out (disclosed at the endpoints)."""
    return value.replace(tzinfo=UTC).isoformat() if value is not None else None


def _worker_label(db_url: str) -> str:
    """Who drains the queue, derived from the DB URL (the ``_is_azure`` predicate --
    URL-shaped, never connectivity-shaped): the Azure DB is drained by the cloud
    job every 15 minutes; a local DB has NO scheduled drain, so requests wait for a
    manual ``python -m swing_screener.notify.ondemand`` run (the UI copy says so)."""
    return "cloud (*/15min)" if _is_azure(db_url) else "manual"


def _chart_keys(raw: str) -> list[str]:
    """``AnalysisRequest.chart_blob_keys`` is COMMA-JOINED (String(2048)): split,
    strip, drop empties -- a trailing comma or blank segment is not a chart."""
    return [k.strip() for k in raw.split(",") if k.strip()]


def _pdf_filename(ticker: str) -> str:
    """``{ticker}_report.pdf`` with the ticker reduced to header-safe characters:
    the value is DB-sourced (model-validated on the way in today, but legacy rows
    predate the model) and a quote or CR/LF inside Content-Disposition corrupts the
    header. ASCII alphanumerics plus ``._-`` survive -- ``isalnum`` alone is
    Unicode-aware, so a fullwidth ticker (ＡＭＤ) would sail through the allowlist
    and then blow up starlette's latin-1 header encoding as an unhandled 500,
    inside this sanitizer's own threat model. An emptied ticker reads 'analysis'."""
    safe = "".join(c for c in ticker if c.isascii() and (c.isalnum() or c in "._-"))
    return f"{safe or 'analysis'}_report.pdf"

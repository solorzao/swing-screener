"""GEX options-lab cockpit router (module 2).

Reads the firewalled ``swing_screener.options`` package and its lab-only tables;
it never touches an equity table. GEX *levels* are deterministic facts and leave
as plain dicts; lab *statistics* leave as full Stat dicts. Writes (build a plan,
grade a setup, take/skip, import a CSV) are guarded by X-Cockpit and bump the
action nonce so other windows wake.
"""

import json
from collections.abc import Callable, Iterator
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.cockpit.common import ActionNonce, _finite_or_none, _require_cockpit
from swing_screener.db.models import GexSnapshot, OptionPaperTrade, OptionSetup
from swing_screener.options import broker_import
from swing_screener.options.chain import _now_eastern
from swing_screener.options.config import GexConfig
from swing_screener.options.journal import (
    SettledTradeError,
    create_setup,
    list_recent_setups,
    list_setups,
    set_status,
)
from swing_screener.options.reading import describe_gex
from swing_screener.options.run import BarsFetcher, Snapshotter, run_analyze, run_plan, run_settle
from swing_screener.options.stats import by_grade, lab_summary, open_trade_count, robinhood_summary

_EASTERN = ZoneInfo("America/New_York")

# The routine yfinance/network failure classes on the build/settle POSTs: the
# default fetch seams raise RuntimeError after retry exhaustion / empty bars
# (chain._fetch_chain_raw, run._default_daily/_default_5m), and raw transport
# errors are OSError subclasses (requests' RequestException is an IOError;
# sockets and timeouts too). A dead upstream is a 503 wearing the exception
# CLASS only (preflight.broker_error_detail's leak posture -- fetcher messages
# can embed hosts and URLs); anything outside this set is a genuine bug and
# still 500s. SQLAlchemyError is neither, so DB failures keep riding the
# app-level handler's own 503.
_UPSTREAM_ERRORS = (OSError, RuntimeError)


def _upstream_503(exc: Exception) -> HTTPException:
    """The upstream-failure wire shape: 503, class name only, never the message."""
    return HTTPException(
        status_code=503, detail=f"upstream error ({type(exc).__name__})")


def _num(v: float | None) -> float | None:
    """None-safe finite guard: pass None through, null out inf/nan on real values.
    (common._finite_or_none assumes a real float and raises on None.)"""
    return None if v is None else _finite_or_none(v)


def _lab_iso(value: datetime | None) -> str | None:
    """A lab-table datetime as an ISO string with the correct Eastern offset.

    LAB TIME CONVENTION (options/journal.py module docstring): every lab writer
    stamps NAIVE US/Eastern wall time (``chain._now_eastern`` -- this router's
    create/status stamps route through it too). ``settle.py`` depends on it: bar
    indexes are normalized to naive Eastern and compared against ``opened_at``
    directly, so writers must never stamp UTC or machine-local. Serving therefore
    localizes with ``America/New_York`` (emitting -04:00/-05:00 per DST) instead
    of ``common._utc_iso``, which assumes UTC writers and would render every lab
    timestamp 4-5 hours wrong."""
    return value.replace(tzinfo=_EASTERN).isoformat() if value is not None else None


def _parse_day(day: str | None) -> date | None:
    """A ``YYYY-MM-DD`` query string as a date, or None. A malformed value is a
    422 (client error), never a 500 -- routers/journal.py's ``_parse_month``
    posture."""
    if not day:
        return None
    try:
        return date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="day must be YYYY-MM-DD") from exc


_CHK_KEYS = [
    "chk_daily_bias_clear", "chk_daily_stack_ordered", "chk_m5_agrees",
    "chk_gex_levels_marked", "chk_price_at_pivot", "chk_regime_match",
    "chk_pattern_clean", "chk_volume_confirming", "chk_risk_sized",
    "chk_stop_structural", "chk_rr_at_least_2", "chk_confirmation_candle",
]


class BuildBody(BaseModel):
    ticker: str | None = Field(default=None, max_length=16)


class SetupBody(BaseModel):
    underlying: str = Field(max_length=16)
    direction: str = Field(max_length=8)
    checklist: dict[str, bool]
    entry: float | None = Field(default=None, allow_inf_nan=False)
    stop: float | None = Field(default=None, allow_inf_nan=False)
    target: float | None = Field(default=None, allow_inf_nan=False)
    regime: str = Field(default="unknown", max_length=16)
    pivot_level: float | None = Field(default=None, allow_inf_nan=False)
    pattern: str = Field(default="", max_length=256)
    notes: str = Field(default="", max_length=2048)
    gex_snapshot_id: int | None = None


class StatusBody(BaseModel):
    status: str = Field(max_length=16)


class ParseBody(BaseModel):
    csv_text: str = Field(max_length=5_000_000)


class CommitBody(BaseModel):
    csv_text: str = Field(max_length=5_000_000)
    tags: dict[str, str]


def _profile_rows(profile_json: str | None) -> list[dict[str, float]] | None:
    """The persisted per-strike dollar-gamma profile, or None when absent or
    corrupt — a bad blob degrades the CHART quietly (the level numbers above it
    still render; quiet-degrade is the plan payload's posture)."""
    if not profile_json:
        return None
    try:
        rows = json.loads(profile_json)
        return [
            {
                "strike": float(r["strike"]),
                "call_gex": float(r["call_gex"]),
                "put_gex": float(r["put_gex"]),
            }
            for r in rows
        ]
    except (ValueError, TypeError, KeyError):
        return None


def _snapshot_dict(snap: GexSnapshot) -> dict[str, object]:
    return {
        "underlying": snap.underlying,
        "ts": _lab_iso(snap.ts),
        "spot": _num(snap.spot),
        "call_wall": _num(snap.call_wall),
        "put_wall": _num(snap.put_wall),
        "gamma_flip": _num(snap.gamma_flip),
        "regime": snap.regime,
        "thin_chain": snap.thin_chain,
        "net_gex": _num(snap.net_gex),
        "profile": _profile_rows(snap.profile_json),
        # The deterministic what-this-means lines (options/reading.py) — pure
        # templating over the stored levels; stored rows carry no per-reason
        # thin strings, so the thin line stays generic here.
        "reading": describe_gex(
            spot=snap.spot,
            call_wall=snap.call_wall,
            put_wall=snap.put_wall,
            gamma_flip=snap.gamma_flip,
            regime=snap.regime,
            net_gex=snap.net_gex,
            thin_chain=snap.thin_chain,
        ),
    }


def _plan_dict(plan: object) -> dict[str, object]:
    p = plan  # DayPlan
    return {
        "underlying": p.underlying,          # type: ignore[attr-defined]
        "bias": p.bias,                      # type: ignore[attr-defined]
        "regime": p.regime,                  # type: ignore[attr-defined]
        "call": p.call,                      # type: ignore[attr-defined]
        "spacing_pct": _num(p.spacing_pct),  # type: ignore[attr-defined]
        "call_wall": _num(p.levels.call_wall),   # type: ignore[attr-defined]
        "put_wall": _num(p.levels.put_wall),     # type: ignore[attr-defined]
        "gamma_flip": _num(p.levels.gamma_flip),  # type: ignore[attr-defined]
        "spot": _num(p.levels.spot),             # type: ignore[attr-defined]
    }


def _trade_dict(t: OptionPaperTrade | None) -> dict[str, object] | None:
    """The linked paper trade's outcome (or None for an untaken setup) -- the
    frontend's outcome-chip contract: status/opened_at/exit_reason/realized_r."""
    if t is None:
        return None
    return {
        "status": t.status,
        "opened_at": _lab_iso(t.opened_at),
        "exit_reason": t.exit_reason,
        "realized_r": _num(t.realized_r),
    }


def _setup_dict(
    s: OptionSetup, *, trade: OptionPaperTrade | None = None
) -> dict[str, object]:
    d: dict[str, object] = {
        "id": s.id, "ts": _lab_iso(s.ts), "underlying": s.underlying,
        "direction": s.direction, "regime": s.regime, "grade": s.grade,
        "status": s.status, "pattern": s.pattern, "notes": s.notes,
        "pivot_level": _num(s.pivot_level),
        "entry": _num(s.entry), "stop": _num(s.stop),
        "target": _num(s.target),
        "trade": _trade_dict(trade),
    }
    for key in _CHK_KEYS:
        d[key] = getattr(s, key)
    return d


def _trade_for(session: Session, setup_id: int) -> OptionPaperTrade | None:
    return session.scalar(
        select(OptionPaperTrade).where(OptionPaperTrade.setup_id == setup_id)
    )


def _trades_by_setup(
    session: Session, setup_ids: list[int]
) -> dict[int, OptionPaperTrade]:
    """The linked lab trades for a page of setups, one query (not per-row)."""
    if not setup_ids:
        return {}
    rows = session.scalars(
        select(OptionPaperTrade).where(OptionPaperTrade.setup_id.in_(setup_ids))
    )
    return {t.setup_id: t for t in rows if t.setup_id is not None}


def _episode_dict(e: broker_import.Episode) -> dict[str, object]:
    return {
        "occ_symbol": e.occ_symbol, "underlying": e.underlying,
        "opened_on": e.opened_on.isoformat(),
        "closed_on": e.closed_on.isoformat() if e.closed_on else None,
        "status": e.status, "contracts": e.contracts,
        "entry_premium": _num(e.entry_premium),
        "exit_premium": _num(e.exit_premium),
        "pnl": _num(e.pnl), "exit_reason": e.exit_reason,
        "needs_review": e.needs_review, "import_key": e.import_key,
    }


def build_gex_router(
    *,
    _session: Callable[[], Iterator[Session]],
    action_nonce: ActionNonce,
    snapshotter: Snapshotter | None = None,
    daily_bars: Callable[[str], object] | None = None,
    bars_5m: BarsFetcher | None = None,
) -> APIRouter:
    """``snapshotter`` / ``daily_bars`` are the plan-build test seams and ``bars_5m``
    the settle-sweep one (None binds the real yfinance chain / daily-bar / 5m-bar
    fetches); nothing here touches the network until a build/analyze/settle POST
    asks."""
    router = APIRouter()
    cfg = GexConfig()

    @router.get("/api/gex/plan")
    def plan(session: Session = Depends(_session)) -> dict[str, object]:
        latest: list[dict[str, object]] = []
        for underlying in cfg.watchlist:
            snap = session.scalars(
                select(GexSnapshot).where(GexSnapshot.underlying == underlying)
                .order_by(GexSnapshot.ts.desc()).limit(1)
            ).first()
            if snap is not None:
                latest.append(_snapshot_dict(snap))
        return {"snapshots": latest, "watchlist": list(cfg.watchlist)}

    @router.post("/api/gex/plan/build", dependencies=[Depends(_require_cockpit)])
    def build(body: BuildBody, session: Session = Depends(_session)) -> dict[str, object]:
        """Build the watchlist plans, or analyze one ticker. A routine upstream
        (yfinance) failure is a 503 with the class name only (``_upstream_503``);
        the watchlist path already degrades per ticker inside ``run_plan``."""
        if body.ticker:
            try:
                levels, liq = run_analyze(body.ticker, cfg=cfg, snapshotter=snapshotter,
                                          save=True, session=session)
            except _UPSTREAM_ERRORS as exc:
                raise _upstream_503(exc) from exc
            action_nonce.bump()
            return {"analyzed": {
                "underlying": body.ticker, "regime": levels.regime,
                "call_wall": _num(levels.call_wall),
                "put_wall": _num(levels.put_wall),
                "gamma_flip": _num(levels.gamma_flip),
                "spot": _num(levels.spot),
                "thin_chain": liq.thin, "thin_reasons": liq.reasons,
                "net_gex": _num(levels.net_gex),
                "profile": [
                    {"strike": p.strike, "call_gex": p.call_gex, "put_gex": p.put_gex}
                    for p in levels.profile
                ],
                "reading": describe_gex(
                    spot=levels.spot,
                    call_wall=levels.call_wall,
                    put_wall=levels.put_wall,
                    gamma_flip=levels.gamma_flip,
                    regime=levels.regime,
                    net_gex=levels.net_gex,
                    thin_chain=liq.thin,
                    thin_reasons=liq.reasons,
                ),
            }}
        try:
            plans = run_plan(session, cfg=cfg, snapshotter=snapshotter,
                             daily_bars=daily_bars)  # type: ignore[arg-type]
        except _UPSTREAM_ERRORS as exc:
            raise _upstream_503(exc) from exc
        action_nonce.bump()
        return {"plans": [_plan_dict(p) for p in plans]}

    @router.get("/api/gex/setups")
    def setups(
        day: str | None = None, recent: int = 0,
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """One day's setups (``day`` = YYYY-MM-DD; malformed is 422), or with
        ``recent=1`` the last 7 calendar days newest-first (``day`` is ignored --
        the bounded cockpit history window). Each row carries its linked trade's
        outcome (``trade``: null until taken)."""
        if recent:
            rows = list_recent_setups(session, end_day=_now_eastern().date())
        else:
            rows = list_setups(session, day=_parse_day(day))
        trades = _trades_by_setup(session, [s.id for s in rows])
        return {"setups": [_setup_dict(s, trade=trades.get(s.id)) for s in rows]}

    @router.post("/api/gex/setups", dependencies=[Depends(_require_cockpit)])
    def create(body: SetupBody, session: Session = Depends(_session)) -> dict[str, object]:
        try:
            # Lab convention: stamp naive Eastern (see _lab_iso), never local/UTC.
            row = create_setup(
                session, ts=_now_eastern(), underlying=body.underlying,
                direction=body.direction, checklist=body.checklist,
                entry=body.entry, stop=body.stop, target=body.target,
                regime=body.regime, pivot_level=body.pivot_level,
                pattern=body.pattern, notes=body.notes,
                gex_snapshot_id=body.gex_snapshot_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=f"bad checklist key: {exc}") from exc
        action_nonce.bump()
        return _setup_dict(row)

    @router.post("/api/gex/setups/{setup_id}/status", dependencies=[Depends(_require_cockpit)])
    def status(setup_id: int, body: StatusBody,
               session: Session = Depends(_session)) -> dict[str, object]:
        """404 on an unknown id; 409 when leaving ``taken`` would orphan a SETTLED
        trade (``SettledTradeError`` -- graded history is immutable); the ``at``
        stamp is naive Eastern per the lab convention (see ``_lab_iso``)."""
        if body.status not in {"idea", "taken", "skipped"}:
            raise HTTPException(status_code=422, detail="status must be idea|taken|skipped")
        try:
            row = set_status(session, setup_id, body.status, at=_now_eastern())
        except SettledTradeError as exc:  # BEFORE ValueError: it subclasses it
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:  # unknown setup id
            raise HTTPException(
                status_code=404, detail=f"no setup with id {setup_id}") from exc
        action_nonce.bump()
        return _setup_dict(row, trade=_trade_for(session, setup_id))

    @router.post("/api/gex/import/parse", dependencies=[Depends(_require_cockpit)])
    def import_parse(body: ParseBody,
                     session: Session = Depends(_session)) -> dict[str, object]:
        fills = broker_import.parse_activity_csv(body.csv_text)
        stats = broker_import.store_fills(session, fills)
        episodes = broker_import.pair_episodes(fills)
        action_nonce.bump()
        return {
            "episodes": [_episode_dict(e) for e in episodes],
            "fills_added": stats.added, "fills_skipped": stats.skipped,
        }

    @router.post("/api/gex/import/commit", dependencies=[Depends(_require_cockpit)])
    def import_commit(body: CommitBody,
                      session: Session = Depends(_session)) -> dict[str, object]:
        # Re-parse the same CSV (store_fills is idempotent) so parse and commit can be
        # separate requests without reconstructing fills from the DB.
        fills = broker_import.parse_activity_csv(body.csv_text)
        broker_import.store_fills(session, fills)
        episodes = broker_import.pair_episodes(fills)
        committed = broker_import.commit_episodes(session, episodes, body.tags)
        action_nonce.bump()
        return {"committed": committed}

    @router.post("/api/gex/settle", dependencies=[Depends(_require_cockpit)])
    def settle(session: Session = Depends(_session)) -> dict[str, object]:
        """Sweep due open lab trades through the CLI's settle path (``run_settle``
        -- same logic, not duplicated). Idempotent: nothing due is still a 200
        with ``settled: 0``, never an error. Trades whose underlying has no bars
        yet stay open and count in ``open_remaining`` (``run_settle`` degrades
        per-underlying fetch failures itself); an upstream error that ESCAPES
        the sweep is a 503 with the class name only (``_upstream_503``)."""
        try:
            result = run_settle(session, cfg=cfg, bars_fetcher=bars_5m)
        except _UPSTREAM_ERRORS as exc:
            raise _upstream_503(exc) from exc
        if result.settled:
            action_nonce.bump()  # only when a write actually landed
        return {
            "settled": result.settled,
            "open_remaining": open_trade_count(session),
        }

    @router.get("/api/gex/stats")
    def stats(session: Session = Depends(_session)) -> dict[str, object]:
        return {
            "overall": lab_summary(session, account="options-lab"),
            "by_grade": by_grade(session, account="options-lab"),
            "robinhood": robinhood_summary(session),
            # unsettled lab work: what a POST /api/gex/settle would try to sweep
            "open_trades": open_trade_count(session),
        }

    return router

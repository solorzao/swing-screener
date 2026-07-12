"""GEX options-lab cockpit router (module 2).

Reads the firewalled ``swing_screener.options`` package and its lab-only tables;
it never touches an equity table. GEX *levels* are deterministic facts and leave
as plain dicts; lab *statistics* leave as full Stat dicts. Writes (build a plan,
grade a setup, take/skip, import a CSV) are guarded by X-Cockpit and bump the
action nonce so other windows wake.
"""

from collections.abc import Callable, Iterator
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.cockpit.common import ActionNonce, _finite_or_none, _require_cockpit, _utc_iso
from swing_screener.db.models import GexSnapshot, OptionSetup
from swing_screener.options import broker_import
from swing_screener.options.config import GexConfig
from swing_screener.options.journal import create_setup, list_setups, set_status
from swing_screener.options.run import Snapshotter, run_analyze, run_plan
from swing_screener.options.stats import by_grade, lab_summary, robinhood_summary

def _num(v: float | None) -> float | None:
    """None-safe finite guard: pass None through, null out inf/nan on real values.
    (common._finite_or_none assumes a real float and raises on None.)"""
    return None if v is None else _finite_or_none(v)


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


def _snapshot_dict(snap: GexSnapshot) -> dict[str, object]:
    return {
        "underlying": snap.underlying,
        "ts": _utc_iso(snap.ts),
        "spot": _num(snap.spot),
        "call_wall": _num(snap.call_wall),
        "put_wall": _num(snap.put_wall),
        "gamma_flip": _num(snap.gamma_flip),
        "regime": snap.regime,
        "thin_chain": snap.thin_chain,
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


def _setup_dict(s: OptionSetup) -> dict[str, object]:
    d: dict[str, object] = {
        "id": s.id, "ts": _utc_iso(s.ts), "underlying": s.underlying,
        "direction": s.direction, "regime": s.regime, "grade": s.grade,
        "status": s.status, "pattern": s.pattern, "notes": s.notes,
        "pivot_level": _num(s.pivot_level),
        "entry": _num(s.entry), "stop": _num(s.stop),
        "target": _num(s.target),
    }
    for key in _CHK_KEYS:
        d[key] = getattr(s, key)
    return d


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
) -> APIRouter:
    """``snapshotter`` / ``daily_bars`` are the plan-build test seams (None binds the
    real yfinance chain + daily-bar fetch); nothing here touches the network until a
    build/analyze POST asks."""
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
        if body.ticker:
            levels, liq = run_analyze(body.ticker, cfg=cfg, snapshotter=snapshotter,
                                      save=True, session=session)
            action_nonce.bump()
            return {"analyzed": {
                "underlying": body.ticker, "regime": levels.regime,
                "call_wall": _num(levels.call_wall),
                "put_wall": _num(levels.put_wall),
                "gamma_flip": _num(levels.gamma_flip),
                "spot": _num(levels.spot),
                "thin_chain": liq.thin, "thin_reasons": liq.reasons,
            }}
        plans = run_plan(session, cfg=cfg, snapshotter=snapshotter,
                         daily_bars=daily_bars)  # type: ignore[arg-type]
        action_nonce.bump()
        return {"plans": [_plan_dict(p) for p in plans]}

    @router.get("/api/gex/setups")
    def setups(day: str | None = None, session: Session = Depends(_session)) -> dict[str, object]:
        parsed = date.fromisoformat(day) if day else None
        rows = list_setups(session, day=parsed)
        return {"setups": [_setup_dict(s) for s in rows]}

    @router.post("/api/gex/setups", dependencies=[Depends(_require_cockpit)])
    def create(body: SetupBody, session: Session = Depends(_session)) -> dict[str, object]:
        try:
            row = create_setup(
                session, ts=datetime.now(), underlying=body.underlying,
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
        if body.status not in {"idea", "taken", "skipped"}:
            raise HTTPException(status_code=422, detail="status must be idea|taken|skipped")
        row = set_status(session, setup_id, body.status, at=datetime.now())
        action_nonce.bump()
        return _setup_dict(row)

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

    @router.get("/api/gex/stats")
    def stats(session: Session = Depends(_session)) -> dict[str, object]:
        return {
            "overall": lab_summary(session, account="options-lab"),
            "by_grade": by_grade(session, account="options-lab"),
            "robinhood": robinhood_summary(session),
        }

    return router

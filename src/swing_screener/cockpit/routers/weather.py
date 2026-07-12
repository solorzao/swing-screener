"""The Market Weather screen's read: the latest weekly ``MarketReport`` plus the
flip-log history -- one bounded-table read over the shared session seam, no broker,
no quotes, no LLM (the report text was authored at job time; serving it re-runs
nothing)."""

from collections.abc import Callable, Iterator

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.cockpit.common import _utc_iso
from swing_screener.db.models import MarketReport


def build_weather_router(
    *,
    _session: Callable[[], Iterator[Session]],
) -> APIRouter:
    """The weather endpoint, closed over the app's session seam."""
    router = APIRouter()

    @router.get("/api/weather")
    def weather(session: Session = Depends(_session)) -> dict[str, object]:
        """The latest Market Weather report + the run history, newest first.

        ``weather`` is the newest row by ``run_date`` (unique per date -- the
        idempotency backstop), EVERY column served: the deterministic indicator
        facts, ``core`` (the one-line read), ``report`` (the full analyst text,
        verbatim), and ``is_deep`` -- ``False`` marks the deterministic FALLBACK
        report (no LLM ran; the UI badges it so a canned read is never mistaken
        for the analyst's). ``{"weather": null}`` when no report has ever run --
        a setup state, not an error (the job is weekly; a fresh DB has none).

        ``history`` is the flip log: every run's ``{run_date, ha_alignment,
        flipped, core}`` newest-first (weekly cadence -- the whole table is a few
        rows per month, so no pagination). ``run_date`` doubles as the as-of for
        this weekly-stale data; ``created_at`` rides as unambiguous UTC
        (``_utc_iso``) for the "generated at" caption.
        """
        rows = list(session.scalars(
            select(MarketReport).order_by(MarketReport.run_date.desc(),
                                          MarketReport.id.desc())))
        if not rows:
            return {"weather": None, "history": []}
        latest = rows[0]
        return {
            "weather": {
                "run_date": latest.run_date.isoformat(),
                "ha_alignment": latest.ha_alignment,
                "flipped": latest.flipped,
                "spy_vs_200dma": latest.spy_vs_200dma,
                "vol_bucket": latest.vol_bucket,
                "vix": latest.vix,
                "vix_rank": latest.vix_rank,
                "vix_spike": latest.vix_spike,
                "ten_year": latest.ten_year,
                "three_month": latest.three_month,
                "yield_inverted": latest.yield_inverted,
                "bond_trend": latest.bond_trend,
                "vix_term_ratio": latest.vix_term_ratio,
                "vix_backwardation": latest.vix_backwardation,
                "credit_chg_4w": latest.credit_chg_4w,
                "credit_pctile": latest.credit_pctile,
                "cyc_def_trend": latest.cyc_def_trend,
                "cyc_def_chg_4w": latest.cyc_def_chg_4w,
                "breadth_trend": latest.breadth_trend,
                "breadth_chg_4w": latest.breadth_chg_4w,
                "recession_prob": latest.recession_prob,
                "is_deep": latest.is_deep,
                "core": latest.core,
                "report": latest.report,
                "created_at": _utc_iso(latest.created_at),
            },
            "history": [{
                "run_date": r.run_date.isoformat(),
                "ha_alignment": r.ha_alignment,
                "flipped": r.flipped,
                "core": r.core,
            } for r in rows],
        }

    return router

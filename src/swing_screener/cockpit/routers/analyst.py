"""The Analyst screen's read: per-play-type calibration, nudge attribution, the
call-freshness split, the autonomy gate's calibration progress, and token spend --
all computed from the ``AnalystCall`` table through the SAME pure helpers the
reflection and the autonomy gate use (``reflect.analyst_calibration``,
``analytics.calibration.conviction_calibrated``, ``repo.analyst_call_freshness``),
so every surface tells one story.

HONESTY POSTURE: every R here is the SHADOW BOOK's (the research grid's scored
calls -- no real dollars; the wire says so in ``r_basis``). Zero-count grades ride
as explicit ``n=0`` rows, never dropped; an insufficient-data calibration bound is
``null`` (its true value is -inf, which JSON cannot carry and a lamp must read as
UNKNOWN, never green); spend totals name their own undercount.
"""

from collections.abc import Callable, Iterator
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.analytics.calibration import conviction_calibrated
from swing_screener.analytics.performance import _CLUSTER_FLOOR, MIN_LEADERBOARD_N
from swing_screener.cockpit.common import _finite_or_none
from swing_screener.db.models import AnalystCall
from swing_screener.db.repo import analyst_call_freshness
from swing_screener.pipeline.reflect import (
    _CALIBRATION_ORDER,
    _PLAY_TYPES,
    analyst_calibration,
)

# The spend undercount, stated once: NULL-cost rows are the deterministic/fallback
# path (no model call) and legacy pre-column rows -- they carry no estimate, so the
# sums can only UNDERcount true spend, never inflate it.
_SPEND_NOTE = (
    "calls with no cost estimate (deterministic path / legacy rows) are not "
    "summed -- totals undercount true spend"
)


def build_analyst_router(
    *,
    _session: Callable[[], Iterator[Session]],
) -> APIRouter:
    """The analyst endpoint, closed over the app's session seam."""
    router = APIRouter()

    @router.get("/api/analyst")
    def analyst(session: Session = Depends(_session)) -> dict[str, object]:
        """The analyst's report card, per play type, plus spend.

        Per play type (one ``AnalystCall`` SELECT each; the pure helpers split it):

        * ``calibration``: one row per FINAL conviction grade in the canonical
          order (high/medium/low/avoid -- ``reflect._CALIBRATION_ORDER``), each
          ``{grade, n, mean_r}`` over the SCORED calls. Grades with no scored
          history ride as an explicit ``{n: 0, mean_r: null}`` ("unproven"),
          never omitted -- the same reshape the picks endpoint's ConvictionChip
          reads, with the zero rows made visible.
        * ``nudge``: ``{n, mean_r}`` across the scored calls where the analyst
          MOVED the baseline (final != baseline), or null when none scored --
          the nudge-attribution read (did its moves add R?).
        * ``freshness``: ``repo.analyst_call_freshness`` over ALL the play type's
          calls at today's date -- ``{scored, pending_in_window,
          expired_unfilled}``; ``expired_unfilled / total`` is the unfilled
          fraction (judgments the shadow book never tested). ``today`` (top
          level) is the date the split used.
        * ``progress``: the autonomy gate's calibration test
          (``conviction_calibrated`` -- high must out-earn low with teeth) plus
          the floors it counts toward (``min_per_bucket``, ``cluster_floor``) so
          the UI can render the countdown. ``ci_low`` is null when the true
          value is -inf (insufficient data): JSON cannot carry inf, so null IS
          the wire contract -- mapped explicitly here, not left to the
          serializer's inf default -- and the lamp must read UNKNOWN there,
          never green.

        ``spend``: today / last-7-calendar-days / last-30-calendar-days sums of
        ``est_cost_usd`` (windows include today), plus how many in-window calls
        carried NO estimate and the undercount note. Dollar sums and counts are
        engine facts and ride plain (the funnel precedent); the R figures above
        carry their n alongside. ``r_basis`` labels every R as shadow-book.
        """
        today = date.today()
        play_types: list[dict[str, object]] = []
        for pt in _PLAY_TYPES:
            calls = list(session.scalars(
                select(AnalystCall).where(AnalystCall.play_type == pt)))
            calib = analyst_calibration(calls)
            by_conviction = calib["by_conviction"]
            rows: list[dict[str, object]] = []
            for grade in _CALIBRATION_ORDER:
                scored = by_conviction.get(grade)
                n, mean_r = scored if scored is not None else (0, None)
                rows.append({"grade": grade, "n": n, "mean_r": mean_r})
            nudge = calib["nudge_vs_baseline_r"]
            verdict = conviction_calibrated(calls)
            play_types.append({
                "play_type": pt,
                "calibration": rows,
                "nudge": (None if nudge is None
                          else {"n": nudge[0], "mean_r": nudge[1]}),
                "freshness": analyst_call_freshness(calls, today),
                "progress": {
                    "calibrated": verdict.calibrated,
                    "high_minus_low": verdict.high_minus_low,
                    "ci_low": _finite_or_none(verdict.ci_low),
                    "n_high": verdict.n_high,
                    "n_low": verdict.n_low,
                    "n_clusters_high": verdict.n_clusters_high,
                    "n_clusters_low": verdict.n_clusters_low,
                    "reason": verdict.reason,
                    "min_per_bucket": MIN_LEADERBOARD_N,
                    "cluster_floor": _CLUSTER_FLOOR,
                },
            })
        return {
            "play_types": play_types,
            "spend": _spend(session, today),
            "today": today.isoformat(),
            "r_basis": "shadow-book",
        }

    return router


def _spend(session: Session, today: date) -> dict[str, object]:
    """The three spend windows over one bounded SELECT (30 calendar days including
    today). Rows with a NULL ``est_cost_usd`` are counted, not summed -- the
    undercount is disclosed, never silently absorbed as zero-cost."""
    rows = session.execute(
        select(AnalystCall.created_date, AnalystCall.est_cost_usd)
        .where(AnalystCall.created_date >= today - timedelta(days=29))
    ).all()
    d7_cutoff = today - timedelta(days=6)
    today_usd = d7 = d30 = 0.0
    uncosted = 0
    for created, cost in rows:
        if cost is None:
            uncosted += 1
            continue
        d30 += cost
        if created >= d7_cutoff:
            d7 += cost
        if created == today:
            today_usd += cost
    return {"today_usd": today_usd, "last_7d_usd": d7, "last_30d_usd": d30,
            "uncosted_calls_30d": uncosted, "note": _SPEND_NOTE}

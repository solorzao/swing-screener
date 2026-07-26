"""Analyst router (split from test_api.py): /api/analyst calibration,
freshness, and spend windows."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from swing_screener.analytics.calibration import _CLUSTER_FLOOR, MIN_LEADERBOARD_N
from swing_screener.db.models import (
    AnalystCall,
)
from swing_screener.db.repo import (
    ANALYST_SCORE_WINDOW_DAYS,
    analyst_call_freshness,
)
from tests.cockpit.conftest import (
    _RUN_D,
    _client,
    _client_and_engine,
    _grade_call,
)

ANALYST_KEYS = {"play_types", "spend", "today", "r_basis"}
ANALYST_PT_KEYS = {"play_type", "calibration", "nudge", "freshness", "progress"}
PROGRESS_KEYS = {"calibrated", "high_minus_low", "ci_low", "n_high", "n_low",
                 "n_clusters_high", "n_clusters_low", "reason", "min_per_bucket",
                 "cluster_floor"}
SPEND_KEYS = {"today_usd", "last_7d_usd", "last_30d_usd", "uncosted_calls_30d",
              "note"}
def test_analyst_inf_ci_serializes_null(tmp_path: Path) -> None:
    """Below the data floors ``conviction_calibrated`` answers ``ci_low=-inf``;
    JSON cannot carry inf, so the WIRE CONTRACT is null -- the endpoint maps it
    explicitly (pydantic's json mode would also null it, but the contract must
    not hang on a serializer default) and the lamp reads 'insufficient data',
    never green. Mutation-proof: any mutation that FABRICATES a finite bound
    here (a number where no data exists) fails the ``is None`` pin."""
    r = _client(tmp_path).get("/api/analyst")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == ANALYST_KEYS
    assert body["r_basis"] == "shadow-book"  # every R here is the shadow book's
    assert len(body["play_types"]) == 2
    for pt in body["play_types"]:
        assert set(pt) == ANALYST_PT_KEYS
        p = pt["progress"]
        assert set(p) == PROGRESS_KEYS
        assert p["ci_low"] is None
        assert p["calibrated"] is False
        assert p["reason"].startswith("insufficient data")
        assert p["min_per_bucket"] == MIN_LEADERBOARD_N  # the countdown targets
        assert p["cluster_floor"] == _CLUSTER_FLOOR


def test_calibration_includes_zero_count_grades(tmp_path: Path) -> None:
    """The calibration reshape serves ALL four grades in canonical order: a grade
    with no scored history is an explicit ``{n: 0, mean_r: null}`` ('unproven'),
    never omitted -- dropping the zero rows is the mutation this kills. The
    nudge row aggregates only the MOVED scored calls (final != baseline); an
    unscored call appears in neither."""
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(_grade_call("AAA", final="high", realized_r=0.5, scored_at=_RUN_D))
        s.add(_grade_call("BBB", final="high", realized_r=0.1, scored_at=_RUN_D))
        s.add(_grade_call("CCC", final="high"))  # unscored: rides nowhere
        s.commit()
    body = client.get("/api/analyst").json()
    rev = next(p for p in body["play_types"] if p["play_type"] == "reversal")
    assert rev["calibration"] == [
        {"grade": "high", "n": 2, "mean_r": pytest.approx(0.3)},
        {"grade": "medium", "n": 0, "mean_r": None},
        {"grade": "low", "n": 0, "mean_r": None},
        {"grade": "avoid", "n": 0, "mean_r": None},
    ]
    # both scored calls ARE nudges (the helper's baseline is medium, final high)
    assert rev["nudge"] == {"n": 2, "mean_r": pytest.approx(0.3)}
    cont = next(p for p in body["play_types"] if p["play_type"] == "continuation")
    assert cont["nudge"] is None
    assert all(row["n"] == 0 and row["mean_r"] is None
               for row in cont["calibration"])


def test_unfilled_fraction_three_way(tmp_path: Path) -> None:
    """``freshness`` splits ALL of a play type's calls against the SCORER'S OWN
    window (``ANALYST_SCORE_WINDOW_DAYS`` -- shared constant, agree by
    construction): scored / unscored-but-in-window / expired (the pick never
    filled; unscored forever). Boundary pinned on the pure helper: on the exact
    window-end day a trade can still open, so the call is pending; one day past
    it is expired."""
    today = datetime.now(UTC).date()
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(_grade_call("SCR", run_date=today - timedelta(days=20),
                          realized_r=0.4, scored_at=today - timedelta(days=15)))
        s.add(_grade_call("PEND", run_date=today))
        s.add(_grade_call(
            "EXP",
            run_date=today - timedelta(days=ANALYST_SCORE_WINDOW_DAYS + 1)))
        s.commit()
    body = client.get("/api/analyst").json()
    assert body["today"] == today.isoformat()  # the date the split used
    rev = next(p for p in body["play_types"] if p["play_type"] == "reversal")
    assert rev["freshness"] == {"scored": 1, "pending_in_window": 1,
                                "expired_unfilled": 1}

    edge = AnalystCall(
        created_date=today, ticker="EDGE", timeframe="1d", play_type="reversal",
        run_date=today - timedelta(days=ANALYST_SCORE_WINDOW_DAYS),
        baseline_conviction="medium", final_conviction="high",
        nudge_reason="x", model="m")
    assert analyst_call_freshness([edge], today) == {
        "scored": 0, "pending_in_window": 1, "expired_unfilled": 0}
    assert analyst_call_freshness([edge], today + timedelta(days=1)) == {
        "scored": 0, "pending_in_window": 0, "expired_unfilled": 1}


def test_analyst_spend_windows_and_undercount(tmp_path: Path) -> None:
    """Spend sums ``est_cost_usd`` over calendar windows INCLUDING today (today /
    7d / 30d); a NULL-cost row (deterministic path, legacy) is COUNTED and
    disclosed, never silently summed as zero -- the note says the totals
    undercount. BOTH window edges are pinned with straddling rows (day 6 in /
    day 7 out of the 7d window; day 29 in / day 30 out of the 30d window --
    power-of-two costs so any misassignment changes a sum uniquely), the same
    treatment the freshness split's boundary got. A 40-day-old row is outside
    every window."""
    today = datetime.now(UTC).date()
    client, engine = _client_and_engine(tmp_path)

    def _costed(ticker: str, days_ago: int, cost: float | None) -> AnalystCall:
        call = _grade_call(ticker, run_date=today - timedelta(days=days_ago))
        call.est_cost_usd = cost
        return call

    with Session(engine) as s:
        s.add(_costed("T0", 0, 1.0))
        s.add(_costed("T3", 3, 2.0))
        s.add(_costed("T6", 6, 16.0))     # last day INSIDE the 7d window
        s.add(_costed("T7", 7, 32.0))     # first day OUTSIDE it (30d only)
        s.add(_costed("T10", 10, 4.0))
        s.add(_costed("T29", 29, 64.0))   # last day INSIDE the 30d window
        s.add(_costed("T30", 30, 128.0))  # first day OUTSIDE it
        s.add(_costed("T40", 40, 8.0))    # outside every window
        s.add(_costed("NUL", 0, None))    # no estimate recorded
        s.commit()
    spend = client.get("/api/analyst").json()["spend"]
    assert set(spend) == SPEND_KEYS
    assert spend["today_usd"] == pytest.approx(1.0)
    assert spend["last_7d_usd"] == pytest.approx(19.0)    # 1 + 2 + 16
    assert spend["last_30d_usd"] == pytest.approx(119.0)  # + 32 + 4 + 64
    assert spend["uncosted_calls_30d"] == 1
    assert "undercount" in spend["note"]



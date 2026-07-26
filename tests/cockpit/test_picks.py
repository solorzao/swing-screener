"""Picks router (split from test_api.py): /api/picks digest parity, surfacing
knobs, degraded rows, and analyst grades."""

import dataclasses
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import create_app
from swing_screener.cockpit.routers import picks as picks_module
from swing_screener.config import StrategyConfig
from swing_screener.db.models import (
    Signal,
    Universe,
)
from swing_screener.db.session import get_engine
from swing_screener.notify import select as sel
from swing_screener.notify.run import _drop_already_ran
from tests.cockpit.conftest import (
    _RUN_D,
    _db_url,
    _grade_call,
    _positions_client,
)

# ---- Task 10 reads: /api/picks, /api/ticker, /api/exits, /api/universe, /api/emails


PICKS_KEYS = {"run_date", "daily", "reversal", "extras", "quotes_as_of"}
PICK_ROW_KEYS = {"signal_id", "ticker", "play_type", "timeframe", "horizon", "rank",
                 "score", "strength", "conviction_tier", "entry_floor", "entry_ceiling",
                 "stop", "target", "last_close", "actionability", "is_repeat",
                 "has_chart", "cohort", "analyst"}
_PRIOR_RUN = date(2026, 7, 8)


def _pick_signal(ticker: str, rank: int, *, play_type: str = "reversal",
                 strength: str | None = "confirmed", conviction_tier: str = "base",
                 run_date: date = _RUN_D, first_seen: date | None = _RUN_D,
                 chart: str | None = None, entry_floor: float = 96.0,
                 entry_ceiling: float = 101.0, stop: float = 95.0) -> Signal:
    """One pick-shaped Signal: zone [96, 101], stop 95 (zone-R risk = 6), target 110
    -- at price 100 actionable, 108 extended (dist_r 1.17), 94 broken."""
    return Signal(
        run_date=run_date, ticker=ticker, timeframe="1d", horizon="medium",
        play_type=play_type, strength=strength, conviction_tier=conviction_tier,
        score=0.9, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
        entry_floor=entry_floor, entry_ceiling=entry_ceiling, stop=stop,
        target=110.0, first_seen_date=first_seen, chart_path=chart,
    )


def _digest_reversal_surfaced(engine: Engine, prices: dict[str, float]) -> list[str]:
    """The digest's OWN reversal chain (notify/run.py ~609-622), verbatim: pool ->
    liveness drop (the REAL ``_drop_already_ran``) -> sector cap. The parity tests
    compute their expectation THROUGH this so the endpoint's restated keep-rule
    cannot drift from the digest without a red test."""
    scfg = StrategyConfig()
    with Session(engine) as s:
        pool = sel.reversal_picks(
            s, _RUN_D, top_n=sel.REVERSAL_POOL_N,
            max_age_days=scfg.digest_repeat_cooldown_days,
            premium_only=scfg.reversal_surface_premium_only,
            confirmed_only=scfg.reversal_surface_confirmed_only)
        pool = _drop_already_ran(pool, lambda tickers: prices)
        return [x.ticker for x in sel.cap_signals_by_sector(
            s, pool, max_per_sector=scfg.reversal_max_per_sector, limit=3)]


def test_picks_match_digest_surfaced_set(tmp_path: Path) -> None:
    """PARITY BY CONSTRUCTION: the endpoint's reversal three equal the digest's own
    call chain over the same store -- a liveness-broken pick frees its slot for
    backfill from below the top-3 AND rides as a flagged extra; a cooldown-stale
    pick and an ``early`` pick (confirmed_only bar) appear NOWHERE. R5 has NO
    quote at all, so the fail-open ``unknown`` status flows through BOTH the
    digest chain and the endpoint in this same test. Mutation-proof:
    dropping ``confirmed_only=`` surfaces EARLY (rank 0 -- it would sort FIRST),
    dropping ``max_age_days=`` surfaces STALE, reordering drop-after-cap loses the
    R5 backfill; each diverges from both the computed AND the literal pin."""
    prices = {"R1": 100.0, "R2": 108.0, "R3": 94.0, "R4": 100.0,
              "R6": 100.0, "STALE": 100.0, "EARLY": 100.0}  # R5: quote miss
    client, engine = _positions_client(tmp_path, prices)
    with Session(engine) as s:
        # a prior run anchors the cooldown calendar (cutoff = second-newest run)
        s.add(_pick_signal("OLDRUN", 1, play_type="continuation", strength=None,
                           run_date=_PRIOR_RUN, first_seen=_PRIOR_RUN))
        # R3 (broken) ranks INSIDE the top-3 so its liveness drop frees a slot;
        # R5 (quote miss) ranks 4th and backfills it -- the fail-open pick surfaces.
        for i, t in enumerate(["R1", "R2", "R3", "R5", "R4", "R6"], start=1):
            s.add(_pick_signal(t, i))  # R2 extended at 108 -- NORMAL for a reversal
        s.add(_pick_signal("STALE", 0, first_seen=date(2026, 7, 1)))  # aged out
        s.add(_pick_signal("EARLY", 0, strength="early"))  # confirmed_only bar
        s.commit()

    expected = _digest_reversal_surfaced(engine, prices)
    assert expected == ["R1", "R2", "R5"]  # literal pin: R5 backfilled

    r = client.get("/api/picks")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == PICKS_KEYS
    assert body["run_date"] == _RUN_D.isoformat()
    assert [p["ticker"] for p in body["reversal"]] == expected
    assert body["daily"] == []  # no continuation on the latest run

    extras = {p["ticker"]: p for p in body["extras"]}
    assert set(extras) == {"R3"}  # the liveness-dropped pick rides, flagged
    assert extras["R3"]["actionability"]["status"] == "broken"
    everywhere = {p["ticker"] for p in body["reversal"] + body["extras"]}
    assert {"STALE", "EARLY"}.isdisjoint(everywhere)  # bars, not liveness: no ride

    by_ticker = {p["ticker"]: p for p in body["reversal"]}
    assert by_ticker["R5"]["actionability"]["status"] == "unknown"  # kept, fail-open
    assert by_ticker["R5"]["last_close"] is None
    row = by_ticker["R1"]
    assert set(row) == PICK_ROW_KEYS
    assert row["last_close"] == 100.0
    assert (row["entry_floor"], row["entry_ceiling"], row["stop"], row["target"]) \
        == (96.0, 101.0, 95.0, 110.0)  # the engine's levels VERBATIM
    assert row["cohort"] == {"play_type": "reversal", "strength": "confirmed"}
    assert row["analyst"] is None  # no call recorded for the pick
    assert datetime.fromisoformat(body["quotes_as_of"]).tzinfo is not None


def test_picks_reversal_extended_is_normal(tmp_path: Path) -> None:
    """A reversal above its ceiling is a RESTING LIMIT's normal state: it stays
    SURFACED (status ``extended``); the same price on a continuation is the chase
    -- dropped to extras, and the daily list SHRINKS (the digest drops AFTER its
    cap on the daily side; there is no backfill to invent)."""
    client, engine = _positions_client(tmp_path, {"REV": 108.0, "CONT": 108.0})
    with Session(engine) as s:
        s.add(_pick_signal("REV", 1))
        s.add(_pick_signal("CONT", 1, play_type="continuation", strength=None))
        s.commit()
    body = client.get("/api/picks").json()
    assert [p["ticker"] for p in body["reversal"]] == ["REV"]
    rev = body["reversal"][0]
    assert rev["actionability"]["status"] == "extended"
    assert rev["actionability"]["dist_r"] == pytest.approx((108 - 101) / 6)
    assert body["daily"] == []  # dropped, not backfilled
    assert [(p["ticker"], p["actionability"]["status"]) for p in body["extras"]] \
        == [("CONT", "extended")]


def test_picks_daily_matches_digest_cap_then_drop(tmp_path: Path) -> None:
    """The DAILY side mirrors the digest's order exactly: sector cap INSIDE
    ``daily_picks`` (T3 capped out by the 2-per-sector default, O4 promoted),
    THEN the liveness drop with NO backfill (O4 extended -> two survivors).
    Mutation-proof: dropping ``max_per_sector=`` puts T3 in the three; swapping to
    drop-then-cap backfills a third row (O5); both diverge from the digest chain."""
    prices = {"T1": 100.0, "T2": 100.0, "T3": 100.0, "O4": 108.0, "O5": 100.0,
              "F6": 100.0}
    client, engine = _positions_client(tmp_path, prices)
    sectors = {"T1": "Information Technology", "T2": "Information Technology",
               "T3": "Information Technology", "O4": "Energy", "O5": "Energy",
               "F6": "Financials"}
    with Session(engine) as s:
        for i, t in enumerate(["T1", "T2", "T3", "O4", "O5", "F6"], start=1):
            s.add(_pick_signal(t, i, play_type="continuation", strength=None))
            s.add(Universe(ticker=t, sector=sectors[t]))
        s.commit()

    scfg = StrategyConfig()
    with Session(engine) as s:
        pool = sel.daily_picks(s, _RUN_D,
                               max_age_days=scfg.digest_repeat_cooldown_days,
                               max_per_sector=scfg.daily_max_per_sector)
        expected = [x.ticker for x in _drop_already_ran(pool, lambda t: prices)]
    assert expected == ["T1", "T2"]  # literal pin: capped + shrunk

    body = client.get("/api/picks").json()
    assert [p["ticker"] for p in body["daily"]] == expected
    assert [(p["ticker"], p["actionability"]["status"]) for p in body["extras"]] \
        == [("O4", "extended")]
    everywhere = {p["ticker"] for p in body["daily"] + body["extras"]}
    assert "T3" not in everywhere  # cap loser, not liveness-dropped: no extra ride


def test_picks_surfacing_knobs_follow_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reversal knobs are READ FROM CONFIG per request, not baked from today's
    defaults: with premium_only flipped ON and confirmed_only OFF, only the
    premium early pick surfaces. Mutation-proof both ways: an endpoint that stops
    passing ``premium_only`` surfaces BASEC; one that hardcodes
    ``confirmed_only=True`` (today's default) loses PREME."""
    patched = dataclasses.replace(StrategyConfig(),
                                  reversal_surface_premium_only=True,
                                  reversal_surface_confirmed_only=False)
    monkeypatch.setattr(picks_module, "StrategyConfig", lambda: patched)
    client, engine = _positions_client(tmp_path, {"PREME": 100.0, "BASEC": 100.0})
    with Session(engine) as s:
        s.add(_pick_signal("PREME", 1, strength="early", conviction_tier="premium"))
        s.add(_pick_signal("BASEC", 2, strength="confirmed", conviction_tier="base"))
        s.commit()
    body = client.get("/api/picks").json()
    assert [p["ticker"] for p in body["reversal"]] == ["PREME"]
    assert body["extras"] == []


def test_picks_drop_disabled_follows_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``digest_drop_already_ran`` gates the digest's liveness drop in
    ``notify.run.main`` -- so it gates the endpoint's too: OFF means a broken pick
    stays surfaced (exactly what the email would show) and extras are empty."""
    patched = dataclasses.replace(StrategyConfig(), digest_drop_already_ran=False)
    monkeypatch.setattr(picks_module, "StrategyConfig", lambda: patched)
    client, engine = _positions_client(tmp_path, {"BRK": 94.0})
    with Session(engine) as s:
        s.add(_pick_signal("BRK", 1))
        s.commit()
    body = client.get("/api/picks").json()
    assert [p["ticker"] for p in body["reversal"]] == ["BRK"]
    assert body["reversal"][0]["actionability"]["status"] == "broken"  # still honest
    assert body["extras"] == []


def test_picks_degraded_rows_no_quote_and_bad_zone(tmp_path: Path) -> None:
    """The degraded rows stay rendered, never 503: a quote miss reads
    ``unknown``/null last_close and the pick is KEPT (the digest's fail-open);
    a degenerate zone (stop above ceiling -- no R unit to speak in) also reads
    ``unknown`` instead of dividing by zero. Repeat/chart flags ride the row."""
    client, engine = _positions_client(tmp_path, {"BADZ": 100.0})
    with Session(engine) as s:
        s.add(_pick_signal("ANCHOR", 9, play_type="continuation", strength=None,
                           run_date=_PRIOR_RUN, first_seen=_PRIOR_RUN))
        s.add(_pick_signal("NOQ", 1, first_seen=_PRIOR_RUN, chart="charts/noq.png"))
        s.add(_pick_signal("BADZ", 2, entry_ceiling=95.0, stop=96.0))
        s.commit()
    body = client.get("/api/picks").json()
    rows = {p["ticker"]: p for p in body["reversal"]}
    assert set(rows) == {"NOQ", "BADZ"}  # both kept
    noq = rows["NOQ"]
    assert noq["last_close"] is None
    assert noq["actionability"] == {"status": "unknown", "dist_r": None}
    assert noq["is_repeat"] is True  # first seen on an earlier run
    assert noq["has_chart"] is True
    badz = rows["BADZ"]
    assert badz["last_close"] == 100.0  # the quote is fine; the ZONE is degenerate
    assert badz["actionability"]["status"] == "unknown"
    assert badz["is_repeat"] is False and badz["has_chart"] is False


def test_picks_carry_todays_analyst_grade_with_scored_stats(tmp_path: Path) -> None:
    """The ConvictionChip's data rides each pick: today's AnalystCall grade for the
    pick's (ticker, timeframe, play_type) plus that grade's SCORED record from
    ``analyst_calibration`` -- per play type, so a reversal 'high' never borrows
    continuation history. A grade with no scored calls is an honest n=0/null
    ('unproven'), and a pick with no call carries null (chip absent)."""
    client, engine = _positions_client(
        tmp_path, {"RV": 100.0, "CT": 100.0, "NC": 100.0})
    with Session(engine) as s:
        s.add(_pick_signal("RV", 1))
        s.add(_pick_signal("NC", 2))
        s.add(_pick_signal("CT", 1, play_type="continuation", strength=None))
        s.add(_grade_call("RV", final="high"))  # today's call for RV
        s.add(_grade_call("CT", play_type="continuation", final="medium"))
        # scored reversal history: two 'high' (mean +0.3R), one 'low' (must not bleed)
        for r_, t in ((0.5, "H1"), (0.1, "H2")):
            s.add(_grade_call(t, final="high", run_date=date(2026, 6, 1),
                              realized_r=r_, scored_at=date(2026, 6, 5)))
        s.add(_grade_call("L1", final="low", run_date=date(2026, 6, 1),
                          realized_r=-1.0, scored_at=date(2026, 6, 5)))
        s.commit()
    body = client.get("/api/picks").json()
    rev = {p["ticker"]: p for p in body["reversal"]}
    assert rev["RV"]["analyst"]["grade"] == "high"
    assert rev["RV"]["analyst"]["n"] == 2
    assert rev["RV"]["analyst"]["mean_r"] == pytest.approx(0.3)
    assert rev["NC"]["analyst"] is None  # no call for this pick
    ct = body["daily"][0]
    assert ct["analyst"] == {"grade": "medium", "n": 0, "mean_r": None}  # unproven


def test_picks_empty_db(tmp_path: Path) -> None:
    """No screen run yet is a SETUP state: 200 with null run_date and empty lists,
    never a 404/503 -- and no quote fetch happens (nothing to price)."""
    calls: list[list[str]] = []

    def fetch(tickers: list[str]) -> dict[str, float]:
        calls.append(tickers)
        return {}

    url = _db_url(tmp_path)
    get_engine(url)
    app = create_app(url, edge_dir=tmp_path, latest_closes_fn=fetch,
                     broker_factory=lambda: None)
    body = TestClient(app).get("/api/picks").json()
    assert body == {"run_date": None, "daily": [], "reversal": [], "extras": [],
                    "quotes_as_of": body["quotes_as_of"]}
    assert datetime.fromisoformat(body["quotes_as_of"]).tzinfo is not None
    assert calls == []  # an empty ticker list never reaches upstream



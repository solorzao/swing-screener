import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import create_app
from swing_screener.cockpit.common import ActionNonce, build_engine_seams
from swing_screener.cockpit.routers.gex import build_gex_router
from swing_screener.db.models import GexSnapshot, OptionPaperTrade, OptionSetup
from swing_screener.db.session import get_engine
from swing_screener.options.chain import ChainSnapshot
from swing_screener.options.checklist import CHECKLIST_ITEMS
from swing_screener.options.journal import create_setup
from tests.conftest import make_bars

_HDR = {"X-Cockpit": "1"}
_FIXTURE = Path(__file__).parent.parent / "options" / "fixtures" / "robinhood_sample.csv"
_EASTERN = ZoneInfo("America/New_York")


def _db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"


def _fake_snapshotter(ticker: str, cfg: object) -> ChainSnapshot:
    frame = pd.DataFrame([
        {"expiry": date(2026, 7, 17), "strike": 105.0, "right": "C",
         "open_interest": 50_000, "iv": 0.2},
        {"expiry": date(2026, 7, 17), "strike": 95.0, "right": "P",
         "open_interest": 40_000, "iv": 0.25},
    ])
    return ChainSnapshot(underlying=ticker, spot=100.0,
                         asof=datetime(2026, 7, 13, 9, 10, tzinfo=UTC), frame=frame)


def _fake_daily(ticker: str) -> pd.DataFrame:
    return pd.DataFrame({
        "open": range(100, 220), "high": range(101, 221), "low": range(99, 219),
        "close": range(100, 220), "volume": [1_000_000] * 120,
    }, index=pd.date_range("2026-01-01", periods=120))


def _client(tmp_path: Path, *, seams: bool = False) -> TestClient:
    url = _db_url(tmp_path)
    get_engine(url)
    kwargs: dict[str, object] = {"edge_dir": tmp_path}
    if seams:
        kwargs["gex_snapshotter"] = _fake_snapshotter
        kwargs["gex_daily_bars"] = _fake_daily
    return TestClient(create_app(url, **kwargs))  # type: ignore[arg-type]


def _all_true() -> dict[str, bool]:
    return {i.key: True for i in CHECKLIST_ITEMS}


def test_plan_empty(tmp_path: Path) -> None:
    r = _client(tmp_path).get("/api/gex/plan")
    assert r.status_code == 200
    body = r.json()
    assert body["snapshots"] == []
    assert body["watchlist"] == ["SPY", "QQQ"]


def test_build_requires_cockpit_header(tmp_path: Path) -> None:
    r = _client(tmp_path, seams=True).post("/api/gex/plan/build", json={})
    assert r.status_code == 403


def test_build_watchlist_persists_snapshots(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    client = _client(tmp_path, seams=True)
    r = client.post("/api/gex/plan/build", json={}, headers=_HDR)
    assert r.status_code == 200
    plans = r.json()["plans"]
    assert len(plans) == 2 and {p["underlying"] for p in plans} == {"SPY", "QQQ"}
    with Session(get_engine(url)) as s:
        assert s.query(GexSnapshot).count() == 2


def test_build_single_ticker_analyze(tmp_path: Path) -> None:
    r = _client(tmp_path, seams=True).post(
        "/api/gex/plan/build", json={"ticker": "NVDA"}, headers=_HDR)
    assert r.status_code == 200
    an = r.json()["analyzed"]
    assert an["underlying"] == "NVDA"
    assert an["thin_chain"] is True  # 2-strike fixture trips the guard


def test_build_lowercase_ticker_is_uppercased(tmp_path: Path) -> None:
    # The FE uppercases its input, but the server must not trust it: a probed
    # "nvda" would persist a parallel snapshot row keyed "nvda" that the
    # uppercase-keyed reads (plan watchlist, autograde same-day gate) never see.
    url = _db_url(tmp_path)
    r = _client(tmp_path, seams=True).post(
        "/api/gex/plan/build", json={"ticker": "nvda"}, headers=_HDR)
    assert r.status_code == 200
    assert r.json()["analyzed"]["underlying"] == "NVDA"
    with Session(get_engine(url)) as s:
        snap = s.scalars(select(GexSnapshot)).one()
        assert snap.underlying == "NVDA"


def test_build_upstream_failure_is_a_503_class_name_only(tmp_path: Path) -> None:
    """A routine yfinance outage on the ad-hoc analyze path must leave as the
    cockpit's 503 with the exception CLASS only -- fetcher messages embed hosts
    and URLs (the broker_error_detail leak posture), and a dead upstream is not
    a server bug."""
    def dead_snapshotter(ticker: str, cfg: object) -> ChainSnapshot:
        raise RuntimeError(
            "chain snapshot failed for NVDA after 3 tries: 502 from "
            "https://query1.finance.yahoo.com/v7/finance?crumb=s3cret")
    url = _db_url(tmp_path)
    get_engine(url)
    client = TestClient(create_app(
        url, edge_dir=tmp_path, gex_snapshotter=dead_snapshotter,
        gex_daily_bars=_fake_daily))
    r = client.post("/api/gex/plan/build", json={"ticker": "NVDA"}, headers=_HDR)
    assert r.status_code == 503
    assert r.json()["detail"] == "upstream error (RuntimeError)"
    assert "yahoo" not in r.text and "s3cret" not in r.text  # no message on the wire


def test_build_genuine_bug_still_500s(tmp_path: Path) -> None:
    """The 503 posture covers UPSTREAM failure classes only: a genuine bug in a
    seam (here a TypeError) must still surface as a 500, never masquerade as a
    dead data source."""
    def buggy_snapshotter(ticker: str, cfg: object) -> ChainSnapshot:
        raise TypeError("boom")
    url = _db_url(tmp_path)
    get_engine(url)
    client = TestClient(create_app(
        url, edge_dir=tmp_path, gex_snapshotter=buggy_snapshotter,
        gex_daily_bars=_fake_daily), raise_server_exceptions=False)
    r = client.post("/api/gex/plan/build", json={"ticker": "NVDA"}, headers=_HDR)
    assert r.status_code == 500


def test_grade_and_list_setups(tmp_path: Path) -> None:
    client = _client(tmp_path)
    body = {"underlying": "SPY", "direction": "long", "checklist": _all_true(),
            "entry": 558.0, "stop": 556.5, "target": 565.0, "pattern": "flag"}
    r = client.post("/api/gex/setups", json=body, headers=_HDR)
    assert r.status_code == 200 and r.json()["grade"] == "A+"
    listed = client.get("/api/gex/setups").json()["setups"]
    assert len(listed) == 1 and listed[0]["underlying"] == "SPY"


def test_bad_checklist_key_is_422(tmp_path: Path) -> None:
    body = {"underlying": "SPY", "direction": "long", "checklist": {"chk_bogus": True}}
    r = _client(tmp_path).post("/api/gex/setups", json=body, headers=_HDR)
    assert r.status_code == 422


def test_setup_persists_play_type_and_autograde_json(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    client = _client(tmp_path)
    body = {"underlying": "SPY", "direction": "long", "checklist": _all_true(),
            "entry": 558.0, "stop": 556.5, "target": 565.0,
            "play_type": "breakout", "autograde_json": '{"machine_verdict": "yes"}'}
    created = client.post("/api/gex/setups", json=body, headers=_HDR)
    assert created.status_code == 200
    assert created.json()["play_type"] == "breakout"
    # provenance lands in the DB row (it is not echoed on the lean list payload)
    with Session(get_engine(url)) as s:
        from swing_screener.db.models import OptionSetup
        row = s.scalars(select(OptionSetup)).one()
        assert row.play_type == "breakout"
        assert row.autograde_json == '{"machine_verdict": "yes"}'


def test_rr_tick_contradicting_levels_is_422(tmp_path: Path) -> None:
    # A ticked chk_rr_at_least_2 whose levels compute R:R < 2 is a stored lie: the
    # server refuses it with a 422 naming the contradiction, never persists it.
    url = _db_url(tmp_path)
    body = {"underlying": "SPY", "direction": "long", "checklist": _all_true(),
            "entry": 100.0, "stop": 98.0, "target": 101.0}  # R:R 0.5 < 2
    r = _client(tmp_path).post("/api/gex/setups", json=body, headers=_HDR)
    assert r.status_code == 422
    assert "R:R" in r.json()["detail"]
    with Session(get_engine(url)) as s:
        from swing_screener.db.models import OptionSetup
        assert list(s.scalars(select(OptionSetup))) == []


def test_rr_tick_ok_when_levels_missing(tmp_path: Path) -> None:
    # The tick stands with no levels typed -- only a demonstrable contradiction is a 422.
    body = {"underlying": "SPY", "direction": "long", "checklist": _all_true()}
    r = _client(tmp_path).post("/api/gex/setups", json=body, headers=_HDR)
    assert r.status_code == 200


def test_rr_tick_side_insane_ordering_is_422(tmp_path: Path) -> None:
    # LONG with stop above entry: abs ratio is 10 but the levels are side-insane
    # (_item_rr's FAIL) -- the ticked box must 422, never store as A+.
    url = _db_url(tmp_path)
    body = {"underlying": "SPY", "direction": "long", "checklist": _all_true(),
            "entry": 100.0, "stop": 110.0, "target": 200.0}
    r = _client(tmp_path).post("/api/gex/setups", json=body, headers=_HDR)
    assert r.status_code == 422
    assert "R:R" in r.json()["detail"]
    with Session(get_engine(url)) as s:
        assert list(s.scalars(select(OptionSetup))) == []


def test_rr_tick_zero_risk_is_422(tmp_path: Path) -> None:
    # entry == stop: undefined R:R can never honestly claim >= 2 (no inf escape).
    body = {"underlying": "SPY", "direction": "long", "checklist": _all_true(),
            "entry": 100.0, "stop": 100.0, "target": 300.0}
    r = _client(tmp_path).post("/api/gex/setups", json=body, headers=_HDR)
    assert r.status_code == 422
    assert "R:R" in r.json()["detail"]


def test_setup_bad_play_type_is_422(tmp_path: Path) -> None:
    # The write path enforces the same enum-or-empty the autograde read does --
    # a free-text play_type would poison the machine-vs-human discipline facet.
    url = _db_url(tmp_path)
    body = {"underlying": "SPY", "direction": "long", "checklist": _all_true(),
            "play_type": "bananas"}
    r = _client(tmp_path).post("/api/gex/setups", json=body, headers=_HDR)
    assert r.status_code == 422
    assert "play_type must be breakout|range or empty" in r.json()["detail"]
    with Session(get_engine(url)) as s:
        assert list(s.scalars(select(OptionSetup))) == []


def test_take_a_setup_transitions_status(tmp_path: Path) -> None:
    client = _client(tmp_path)
    body = {"underlying": "SPY", "direction": "long", "checklist": _all_true(),
            "entry": 558.0, "stop": 556.5, "target": 565.0}
    setup_id = client.post("/api/gex/setups", json=body, headers=_HDR).json()["id"]
    r = client.post(f"/api/gex/setups/{setup_id}/status", json={"status": "taken"}, headers=_HDR)
    assert r.status_code == 200 and r.json()["status"] == "taken"


def test_import_parse_then_commit(tmp_path: Path) -> None:
    client = _client(tmp_path)
    text = _FIXTURE.read_text(encoding="utf-8")
    parsed = client.post("/api/gex/import/parse", json={"csv_text": text}, headers=_HDR)
    assert parsed.status_code == 200
    episodes = parsed.json()["episodes"]
    assert parsed.json()["fills_added"] > 0 and len(episodes) > 0
    closed = [e for e in episodes if e["status"] == "closed"]
    tags = {closed[0]["import_key"]: "gex"}
    committed = client.post("/api/gex/import/commit",
                            json={"csv_text": text, "tags": tags}, headers=_HDR)
    assert committed.status_code == 200 and committed.json()["committed"] == 1
    # re-commit is idempotent
    again = client.post("/api/gex/import/commit",
                        json={"csv_text": text, "tags": tags}, headers=_HDR)
    assert again.json()["committed"] == 0


def test_stats_overall_is_a_stat_dict(tmp_path: Path) -> None:
    stat = _client(tmp_path).get("/api/gex/stats").json()["overall"]
    assert set(stat) >= {"value", "n", "n_clusters", "ci_low", "ci_high",
                         "cost_level", "corpus_id", "facet", "unit", "thin_clusters"}


def test_dead_db_is_503(tmp_path: Path) -> None:
    url = "sqlite:////nonexistent-dir/nope.db"
    client = TestClient(create_app(url, edge_dir=tmp_path), raise_server_exceptions=False)
    for path in ("/api/gex/plan", "/api/gex/stats"):
        assert client.get(path).status_code == 503


# ---------------------------------------------------------------------------
# friendly failures (garbage day / unknown id)

def test_garbage_day_param_is_422_not_500(tmp_path: Path) -> None:
    r = _client(tmp_path).get("/api/gex/setups", params={"day": "garbage"})
    assert r.status_code == 422
    assert "YYYY-MM-DD" in r.json()["detail"]


def test_unknown_setup_id_status_is_404(tmp_path: Path) -> None:
    r = _client(tmp_path).post("/api/gex/setups/999/status",
                               json={"status": "taken"}, headers=_HDR)
    assert r.status_code == 404
    assert "no setup with id 999" in r.json()["detail"]


# ---------------------------------------------------------------------------
# lab timestamp convention: naive Eastern in the DB, Eastern offset on the wire

def test_snapshot_ts_serves_the_eastern_offset(tmp_path: Path) -> None:
    client = _client(tmp_path, seams=True)
    client.post("/api/gex/plan/build", json={}, headers=_HDR)
    snaps = client.get("/api/gex/plan").json()["snapshots"]
    # chain.py stamps naive Eastern (2026-07-13 09:10 in the fixture); the wire
    # form must carry EDT's -04:00, never _utc_iso's +00:00 (a 4h skew).
    assert snaps[0]["ts"] == "2026-07-13T09:10:00-04:00"


def test_setup_ts_is_stamped_and_served_as_eastern_wall_time(tmp_path: Path) -> None:
    client = _client(tmp_path)
    body = {"underlying": "SPY", "direction": "long", "checklist": _all_true()}
    ts = client.post("/api/gex/setups", json=body, headers=_HDR).json()["ts"]
    parsed = datetime.fromisoformat(ts)
    now_eastern = datetime.now(tz=_EASTERN)
    assert parsed.utcoffset() == now_eastern.utcoffset()
    assert abs((parsed - now_eastern).total_seconds()) < 120


# ---------------------------------------------------------------------------
# setup rows carry the linked trade's outcome

def _graded_body() -> dict[str, object]:
    return {"underlying": "SPY", "direction": "long", "checklist": _all_true(),
            "entry": 558.0, "stop": 556.5, "target": 565.0}


def test_setup_rows_carry_the_trade_outcome(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post("/api/gex/setups", json=_graded_body(), headers=_HDR).json()
    assert created["trade"] is None
    taken = client.post(f"/api/gex/setups/{created['id']}/status",
                        json={"status": "taken"}, headers=_HDR).json()
    trade = taken["trade"]
    assert trade["status"] == "open" and trade["opened_at"] is not None
    assert trade["exit_reason"] is None and trade["realized_r"] is None
    listed = client.get("/api/gex/setups").json()["setups"]
    assert listed[0]["trade"]["status"] == "open"


def test_untake_deletes_the_open_trade(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    client = _client(tmp_path)
    setup_id = client.post("/api/gex/setups", json=_graded_body(),
                           headers=_HDR).json()["id"]
    client.post(f"/api/gex/setups/{setup_id}/status",
                json={"status": "taken"}, headers=_HDR)
    skipped = client.post(f"/api/gex/setups/{setup_id}/status",
                          json={"status": "skipped"}, headers=_HDR)
    assert skipped.status_code == 200 and skipped.json()["trade"] is None
    with Session(get_engine(url)) as s:
        assert list(s.scalars(select(OptionPaperTrade))) == []


def test_untake_after_settlement_is_409(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    client = _client(tmp_path)
    setup_id = client.post("/api/gex/setups", json=_graded_body(),
                           headers=_HDR).json()["id"]
    client.post(f"/api/gex/setups/{setup_id}/status",
                json={"status": "taken"}, headers=_HDR)
    with Session(get_engine(url)) as s:
        trade = s.scalars(select(OptionPaperTrade)).one()
        trade.status = "closed"
        trade.exit_reason = "target"
        trade.realized_r = 2.0
        s.commit()
    r = client.post(f"/api/gex/setups/{setup_id}/status",
                    json={"status": "skipped"}, headers=_HDR)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# settle sweep + open-trade count

def _settle_client(tmp_path: Path, bars: pd.DataFrame) -> tuple[TestClient, str]:
    """Router mounted directly so the 5m-bars seam (create_app doesn't thread it)
    can be injected; everything else matches the full app's wiring."""
    url = _db_url(tmp_path)
    get_engine(url)  # create tables
    _engine, _session = build_engine_seams(url)
    app = FastAPI()
    app.include_router(build_gex_router(
        _session=_session, action_nonce=ActionNonce(), bars_5m=lambda ticker: bars,
    ))
    return TestClient(app), url


def _target_hit_bars() -> pd.DataFrame:
    # A full session on TODAY's ET date (09:30..15:55): the sweep's stale-frame
    # guard refuses frames that predate the trade's session day, and the lab
    # stamps opened_at with the real naive-ET clock. Every bar carries the target
    # touch (and stays clear of the 556.5 stop) so the sweep settles no matter
    # what wall time the test runs at.
    day = datetime.now(tz=_EASTERN).date()
    idx = pd.date_range(f"{day} 09:30", f"{day} 15:55", freq="5min")
    n = len(idx)
    return pd.DataFrame({
        "open": [560.0] * n, "high": [566.0] * n,
        "low": [559.5] * n, "close": [565.0] * n,
    }, index=idx)


def test_settle_endpoint_sweeps_due_trades_and_is_idempotent(tmp_path: Path) -> None:
    client, _url = _settle_client(tmp_path, _target_hit_bars())
    setup_id = client.post("/api/gex/setups", json=_graded_body(),
                           headers=_HDR).json()["id"]
    client.post(f"/api/gex/setups/{setup_id}/status",
                json={"status": "taken"}, headers=_HDR)
    r = client.post("/api/gex/settle", headers=_HDR)
    assert r.status_code == 200
    assert r.json() == {"settled": 1, "skipped_incomplete_session": 0, "open_remaining": 0}
    # idempotent sweep: nothing due is still a 200, not an error
    again = client.post("/api/gex/settle", headers=_HDR)
    assert again.status_code == 200
    assert again.json() == {"settled": 0, "skipped_incomplete_session": 0, "open_remaining": 0}
    listed = client.get("/api/gex/setups").json()["setups"]
    assert listed[0]["trade"]["status"] == "closed"
    assert listed[0]["trade"]["exit_reason"] == "target"
    assert listed[0]["trade"]["realized_r"] is not None


def test_settle_requires_cockpit_header(tmp_path: Path) -> None:
    client, _ = _settle_client(tmp_path, _target_hit_bars())
    assert client.post("/api/gex/settle").status_code == 403


def test_settle_upstream_failure_is_a_503_class_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_settle already degrades per-underlying fetch failures internally
    (trades stay open, 200 with settled: 0); this pins the wire posture should
    an upstream error ESCAPE the sweep: the cockpit's 503 with the class name
    only, never the message (it can embed hosts and URLs)."""
    client, _ = _settle_client(tmp_path, _target_hit_bars())

    def dead_settle(*args: object, **kwargs: object) -> object:
        raise ConnectionError("dial query1.finance.yahoo.com:443: timed out")

    monkeypatch.setattr("swing_screener.cockpit.routers.gex.run_settle", dead_settle)
    r = client.post("/api/gex/settle", headers=_HDR)
    assert r.status_code == 503
    assert r.json()["detail"] == "upstream error (ConnectionError)"
    assert "yahoo" not in r.text  # no message text on the wire


def test_stats_payload_carries_open_trade_count(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/api/gex/stats").json()["open_trades"] == 0
    setup_id = client.post("/api/gex/setups", json=_graded_body(),
                           headers=_HDR).json()["id"]
    client.post(f"/api/gex/setups/{setup_id}/status",
                json={"status": "taken"}, headers=_HDR)
    assert client.get("/api/gex/stats").json()["open_trades"] == 1


# ---------------------------------------------------------------------------
# recent window

def test_recent_setups_returns_last_seven_days_and_ignores_day(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    client = _client(tmp_path)
    now = datetime.now(tz=_EASTERN).replace(tzinfo=None)  # the lab's naive-Eastern clock
    with Session(get_engine(url)) as s:
        create_setup(s, ts=now - timedelta(days=10), underlying="OLD",
                     direction="long", checklist=_all_true())
        create_setup(s, ts=now - timedelta(days=2), underlying="MID",
                     direction="long", checklist=_all_true())
        create_setup(s, ts=now, underlying="NEW", direction="long",
                     checklist=_all_true())
    r = client.get("/api/gex/setups",
                   params={"recent": 1, "day": (now - timedelta(days=10)).date().isoformat()})
    assert r.status_code == 200
    assert [x["underlying"] for x in r.json()["setups"]] == ["NEW", "MID"]


def test_plan_snapshots_carry_profile_reading_and_net(tmp_path: Path) -> None:
    """The Day Plan wire serves the persisted per-strike profile, the
    deterministic reading lines, and net_gex -- the chart + callout contract."""
    client = _client(tmp_path, seams=True)
    client.post("/api/gex/plan/build", json={}, headers=_HDR)
    snaps = client.get("/api/gex/plan").json()["snapshots"]
    assert len(snaps) == 2
    for snap in snaps:
        assert isinstance(snap["net_gex"], float)
        profile = snap["profile"]
        assert profile is not None and len(profile) == 2  # the 2-strike fixture
        assert {"strike", "call_gex", "put_gex"} <= set(profile[0])
        reading = snap["reading"]
        assert isinstance(reading, list) and len(reading) >= 2
        # The 2-strike fixture trips the populated-strikes floor: thin first.
        assert reading[0].startswith("THIN CHAIN")
        assert reading[-1].startswith("Model:")


def test_plan_snapshot_corrupt_profile_degrades_quietly(tmp_path: Path) -> None:
    """A corrupt profile_json nulls the chart, never 500s the plan -- the level
    numbers and the reading still serve."""
    url = _db_url(tmp_path)
    with Session(get_engine(url)) as s:
        s.add(GexSnapshot(
            underlying="SPY", ts=datetime(2026, 7, 13, 9, 10, tzinfo=UTC), spot=100.0,
            call_wall=105.0, put_wall=95.0, gamma_flip=99.0, net_gex=1.0,
            regime="positive", profile_json="{not json", thin_chain=False,
            source="computed",
        ))
        s.commit()
    r = _client(tmp_path).get("/api/gex/plan")
    assert r.status_code == 200
    snap = r.json()["snapshots"][0]
    assert snap["profile"] is None
    assert snap["call_wall"] == 105.0
    assert snap["reading"][0].startswith("Positive gamma")


def test_analyze_carries_profile_and_reading(tmp_path: Path) -> None:
    r = _client(tmp_path, seams=True).post(
        "/api/gex/plan/build", json={"ticker": "NVDA"}, headers=_HDR)
    an = r.json()["analyzed"]
    assert len(an["profile"]) == 2
    assert an["reading"][0].startswith("THIN CHAIN")
    # The analyze path DOES know the per-reason strings -- they ride the line.
    assert "populated strikes" in an["reading"][0]
    assert isinstance(an["net_gex"], float)


# ---------------------------------------------------------------------------
# POST /api/gex/autograde -- machine pre-grade with degrade-honest verdicts

def _autograde_client(
    tmp_path: Path, *,
    daily: object | None = None, bars_5m: object | None = None,
    analyzer: object | None = None,
) -> tuple[TestClient, str]:
    """Router mounted directly so the autograde daily / 5m / analyzer seams can be
    injected (create_app threads only the plan-build daily seam); no network."""
    url = _db_url(tmp_path)
    get_engine(url)  # create tables
    _engine, _session = build_engine_seams(url)
    app = FastAPI()
    app.include_router(build_gex_router(
        _session=_session, action_nonce=ActionNonce(),
        daily_bars=daily, bars_5m=bars_5m,  # type: ignore[arg-type]
        autograde_analyzer=analyzer,  # type: ignore[arg-type]
    ))
    return TestClient(app), url


def _uptrend_daily_frame() -> pd.DataFrame:
    closes = [100.0 + 0.5 * i for i in range(150)]
    return pd.DataFrame(
        {"open": closes, "high": [c + 0.1 for c in closes],
         "low": [c - 0.1 for c in closes], "close": closes,
         "volume": [1_000_000.0] * 150},
        index=pd.date_range("2026-01-01", periods=150, freq="D"),
    )


def _uptrend_5m_frame() -> pd.DataFrame:
    # Dated three days back so every bar is COMPLETE regardless of the wall clock at
    # which the test runs (the endpoint grades against the real _now_eastern); the
    # last bar is a green volume spike (confirms + volume-confirming for a long).
    day = datetime.now(tz=_EASTERN).date() - timedelta(days=3)
    n = 105
    closes = [100.0 + 0.5 * i for i in range(n)]
    vols = [1_000_000.0] * n
    vols[-1] = 3_000_000.0
    rows = [
        {"open": c - 0.2, "high": c + 0.1, "low": c - 0.3, "close": c, "volume": v}
        for c, v in zip(closes, vols, strict=True)
    ]
    return make_bars(rows, start=f"{day} 09:30", freq="5min")


def _seed_today_snapshot(url: str, *, underlying: str = "SPY",
                         regime: str = "negative") -> None:
    with Session(get_engine(url)) as s:
        s.add(GexSnapshot(
            underlying=underlying,
            ts=datetime.now(tz=_EASTERN).replace(tzinfo=None),  # naive-Eastern, today
            spot=101.0, call_wall=105.0, put_wall=99.0, gamma_flip=100.5,
            net_gex=1.0, regime=regime, profile_json="[]", thin_chain=False,
            source="computed",
        ))
        s.commit()


def _ag_body(**kw: object) -> dict[str, object]:
    body: dict[str, object] = {
        "underlying": "SPY", "direction": "long", "play_type": "breakout",
        "entry": 100.0, "stop": 99.0, "target": 103.0,
    }
    body.update(kw)
    return body


def test_autograde_happy_path_all_eight_pass(tmp_path: Path) -> None:
    client, url = _autograde_client(
        tmp_path, daily=lambda t: _uptrend_daily_frame(),
        bars_5m=lambda t: _uptrend_5m_frame())
    _seed_today_snapshot(url)
    r = client.post("/api/gex/autograde", json=_ag_body(), headers=_HDR)
    assert r.status_code == 200
    data = r.json()
    assert data["underlying"] == "SPY"
    assert data["machine_verdict"] == "yes"
    assert len(data["items"]) == 8
    assert all(it["state"] == "pass" for it in data["items"])
    assert {"key", "state", "fact"} == set(data["items"][0])
    assert len(data["hints"]) == 2
    assert data["snapshot"] is not None and data["snapshot"]["regime"] == "negative"
    # server-built provenance: items + hints + the cfg thresholds + a stamp
    prov = json.loads(data["autograde_json"])
    assert prov["machine_verdict"] == "yes"
    assert len(prov["items"]) == 8
    assert prov["thresholds"]["rr_min"] == 2.0
    assert prov["thresholds"]["vol_confirm_mult"] == 1.5
    assert prov["ts"]


def test_autograde_cold_ticker_runs_analyzer_exactly_once(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_analyzer(ticker: str, *, cfg: object, save: bool,
                      session: Session) -> tuple[object, object]:
        calls.append(ticker)
        # persist a same-day snapshot, as run_analyze(save=True) would
        session.add(GexSnapshot(
            underlying=ticker, ts=datetime.now(tz=_EASTERN).replace(tzinfo=None),
            spot=101.0, call_wall=105.0, put_wall=99.0, gamma_flip=100.5,
            net_gex=1.0, regime="negative", profile_json="[]", thin_chain=False,
            source="computed"))
        session.commit()
        return object(), object()  # (levels, liq) -- run_autograde ignores the return

    client, _ = _autograde_client(
        tmp_path, daily=lambda t: _uptrend_daily_frame(),
        bars_5m=lambda t: _uptrend_5m_frame(), analyzer=fake_analyzer)
    r = client.post("/api/gex/autograde", json=_ag_body(), headers=_HDR)
    assert r.status_code == 200
    assert calls == ["SPY"]  # cold ticker auto-analyzed exactly once
    data = r.json()
    assert data["snapshot"] is not None  # the freshly analyzed snapshot is used
    assert data["machine_verdict"] == "yes"


def test_autograde_fetch_failure_is_200_incomplete(tmp_path: Path) -> None:
    def dead(ticker: str) -> pd.DataFrame:
        raise RuntimeError(f"no bars for {ticker} (query1.finance.yahoo.com)")

    client, url = _autograde_client(tmp_path, daily=dead, bars_5m=dead)
    _seed_today_snapshot(url)  # snapshot-backed items still grade
    r = client.post("/api/gex/autograde", json=_ag_body(), headers=_HDR)
    assert r.status_code == 200
    data = r.json()
    assert data["machine_verdict"] == "incomplete"  # gaps, no fabricated fail
    by = {it["key"]: it for it in data["items"]}
    assert by["chk_daily_bias_clear"]["state"] == "unavailable"
    assert by["chk_m5_agrees"]["state"] == "unavailable"
    assert by["chk_gex_levels_marked"]["state"] == "pass"  # from the seeded snapshot
    assert "yahoo" not in r.text  # a degraded item never leaks the fetcher's host


def test_autograde_bad_direction_is_422(tmp_path: Path) -> None:
    client, _ = _autograde_client(tmp_path)
    r = client.post("/api/gex/autograde",
                    json={"underlying": "SPY", "direction": "buy"}, headers=_HDR)
    assert r.status_code == 422


def test_autograde_empty_underlying_is_422(tmp_path: Path) -> None:
    client, _ = _autograde_client(tmp_path)
    r = client.post("/api/gex/autograde",
                    json={"underlying": "   ", "direction": "long"}, headers=_HDR)
    assert r.status_code == 422


def test_autograde_requires_cockpit_header(tmp_path: Path) -> None:
    client, _ = _autograde_client(tmp_path)
    r = client.post("/api/gex/autograde", json={"underlying": "SPY", "direction": "long"})
    assert r.status_code == 403


def test_autograde_lowercase_underlying_is_uppercased(tmp_path: Path) -> None:
    # "spy" must not auto-analyze and persist a parallel snapshot row keyed "spy":
    # the server uppercases after strip, so the analyzer sees "SPY" and the
    # response + provenance carry the canonical key.
    calls: list[str] = []

    def fake_analyzer(ticker: str, *, cfg: object, save: bool,
                      session: Session) -> tuple[object, object]:
        calls.append(ticker)
        session.add(GexSnapshot(
            underlying=ticker, ts=datetime.now(tz=_EASTERN).replace(tzinfo=None),
            spot=101.0, call_wall=105.0, put_wall=99.0, gamma_flip=100.5,
            net_gex=1.0, regime="negative", profile_json="[]", thin_chain=False,
            source="computed"))
        session.commit()
        return object(), object()

    client, url = _autograde_client(
        tmp_path, daily=lambda t: _uptrend_daily_frame(),
        bars_5m=lambda t: _uptrend_5m_frame(), analyzer=fake_analyzer)
    r = client.post("/api/gex/autograde", json=_ag_body(underlying=" spy "),
                    headers=_HDR)
    assert r.status_code == 200
    assert calls == ["SPY"]
    assert r.json()["underlying"] == "SPY"
    with Session(get_engine(url)) as s:
        assert s.scalars(select(GexSnapshot)).one().underlying == "SPY"


def test_autograde_bad_play_type_is_422(tmp_path: Path) -> None:
    client, _ = _autograde_client(tmp_path)
    r = client.post("/api/gex/autograde", json=_ag_body(play_type="bananas"),
                    headers=_HDR)
    assert r.status_code == 422
    assert "play_type must be breakout|range or empty" in r.json()["detail"]


def test_autograde_failed_auto_analyze_degrades_to_incomplete(tmp_path: Path) -> None:
    # Cold ticker + dead upstream on the auto-analyze: the snapshot-backed items
    # degrade to 'unavailable' and the read still 200s incomplete -- never a 503.
    def dead_analyzer(ticker: str, *, cfg: object, save: bool,
                      session: Session) -> tuple[object, object]:
        raise RuntimeError("chain snapshot failed (query1.finance.yahoo.com)")

    client, _ = _autograde_client(
        tmp_path, daily=lambda t: _uptrend_daily_frame(),
        bars_5m=lambda t: _uptrend_5m_frame(), analyzer=dead_analyzer)
    r = client.post("/api/gex/autograde", json=_ag_body(), headers=_HDR)
    assert r.status_code == 200
    data = r.json()
    assert data["machine_verdict"] == "incomplete"
    assert data["snapshot"] is None
    by = {it["key"]: it for it in data["items"]}
    assert by["chk_gex_levels_marked"]["state"] == "unavailable"
    assert "yahoo" not in r.text  # the failure message never leaks to the wire


def test_autograde_does_not_bump_nonce_but_setups_does(tmp_path: Path) -> None:
    # Autograde is READ-shaped (no other window needs waking); journaling a setup
    # is a write and must wake them. Pin the pair so neither regresses.
    url = _db_url(tmp_path)
    get_engine(url)
    _engine, _session = build_engine_seams(url)
    nonce = ActionNonce()
    app = FastAPI()
    app.include_router(build_gex_router(
        _session=_session, action_nonce=nonce,
        daily_bars=lambda t: _uptrend_daily_frame(),  # type: ignore[arg-type]
        bars_5m=lambda t: _uptrend_5m_frame(),
    ))
    client = TestClient(app)
    _seed_today_snapshot(url)
    assert client.post("/api/gex/autograde", json=_ag_body(),
                       headers=_HDR).status_code == 200
    assert nonce.value == 0  # a read never wakes the other windows
    body = {"underlying": "SPY", "direction": "long", "checklist": _all_true(),
            "entry": 100.0, "stop": 99.0, "target": 103.0}
    assert client.post("/api/gex/setups", json=body, headers=_HDR).status_code == 200
    assert nonce.value == 1  # the write does


def test_autograde_json_round_trips_into_a_setup_row(tmp_path: Path) -> None:
    client, url = _autograde_client(
        tmp_path, daily=lambda t: _uptrend_daily_frame(),
        bars_5m=lambda t: _uptrend_5m_frame())
    _seed_today_snapshot(url)
    ag = client.post("/api/gex/autograde", json=_ag_body(), headers=_HDR).json()
    prov = ag["autograde_json"]
    assert isinstance(prov, str)
    # the FE echoes the provenance back verbatim when journaling the setup
    body = {"underlying": "SPY", "direction": "long", "checklist": _all_true(),
            "entry": 100.0, "stop": 99.0, "target": 103.0,
            "play_type": "breakout", "autograde_json": prov}
    created = client.post("/api/gex/setups", json=body, headers=_HDR)
    assert created.status_code == 200
    with Session(get_engine(url)) as s:
        row = s.scalars(select(OptionSetup)).one()
        assert row.autograde_json == prov
        assert row.play_type == "breakout"

from datetime import date, datetime, timedelta
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
from swing_screener.db.models import GexSnapshot, OptionPaperTrade
from swing_screener.db.session import get_engine
from swing_screener.options.chain import ChainSnapshot
from swing_screener.options.checklist import CHECKLIST_ITEMS
from swing_screener.options.journal import create_setup

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
                         asof=datetime(2026, 7, 13, 9, 10), frame=frame)


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
    idx = pd.date_range("2026-07-13 09:30", periods=3, freq="5min")
    return pd.DataFrame({
        "open": [558.0, 559.0, 560.0], "high": [559.0, 566.0, 566.0],
        "low": [557.5, 558.5, 559.5], "close": [559.0, 565.5, 565.0],
    }, index=idx)


def test_settle_endpoint_sweeps_due_trades_and_is_idempotent(tmp_path: Path) -> None:
    client, url = _settle_client(tmp_path, _target_hit_bars())
    setup_id = client.post("/api/gex/setups", json=_graded_body(),
                           headers=_HDR).json()["id"]
    client.post(f"/api/gex/setups/{setup_id}/status",
                json={"status": "taken"}, headers=_HDR)
    r = client.post("/api/gex/settle", headers=_HDR)
    assert r.status_code == 200
    assert r.json() == {"settled": 1, "open_remaining": 0}
    # idempotent sweep: nothing due is still a 200, not an error
    again = client.post("/api/gex/settle", headers=_HDR)
    assert again.status_code == 200
    assert again.json() == {"settled": 0, "open_remaining": 0}
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
            underlying="SPY", ts=datetime(2026, 7, 13, 9, 10), spot=100.0,
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

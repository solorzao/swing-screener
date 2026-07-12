from datetime import date, datetime
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import create_app
from swing_screener.db.models import GexSnapshot
from swing_screener.db.session import get_engine
from swing_screener.options.chain import ChainSnapshot
from swing_screener.options.checklist import CHECKLIST_ITEMS

_HDR = {"X-Cockpit": "1"}
_FIXTURE = Path(__file__).parent.parent / "options" / "fixtures" / "robinhood_sample.csv"


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

"""Audit router contract: weekly reports + breach feed reads, and the ack write gate."""

from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import create_app
from swing_screener.db.models import SystemAudit
from swing_screener.db.session import get_engine

_HDR = {"X-Cockpit": "1"}


def _app(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'a.db').as_posix()}"
    engine = get_engine(url)
    return TestClient(create_app(url, edge_dir=tmp_path)), engine


def _seed(engine):
    with Session(engine) as s:
        s.add(SystemAudit(kind="weekly", period_from=date(2026, 7, 6), period_to=date(2026, 7, 12),
                          severity="info", findings_json="{}", generated_at=datetime(2026, 7, 12, 1, 0)))
        s.add(SystemAudit(kind="breach", period_from=date(2026, 7, 8), period_to=date(2026, 7, 8),
                          breach_key="cap:2026-07-08", severity="alert", findings_json="{}",
                          generated_at=datetime(2026, 7, 8, 1, 0)))
        s.commit()
        return s.query(SystemAudit).filter_by(kind="breach").one().id


def test_reports_and_breaches_are_kind_scoped(tmp_path: Path):
    client, engine = _app(tmp_path)
    _seed(engine)
    reports = client.get("/api/audit/reports").json()
    breaches = client.get("/api/audit/breaches").json()
    assert [r["kind"] for r in reports] == ["weekly"]
    assert [b["kind"] for b in breaches] == ["breach"]
    assert breaches[0]["severity"] == "alert" and breaches[0]["acknowledged"] is False


def test_ack_requires_header_and_sets_flag(tmp_path: Path):
    client, engine = _app(tmp_path)
    bid = _seed(engine)
    assert client.post(f"/api/audit/{bid}/ack").status_code == 403        # no header
    ok = client.post(f"/api/audit/{bid}/ack", headers=_HDR)
    assert ok.status_code == 200 and ok.json()["acknowledged"] is True
    assert client.post("/api/audit/9999/ack", headers=_HDR).status_code == 404

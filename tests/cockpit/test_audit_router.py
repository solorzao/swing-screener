"""Audit router contract: weekly reports + breach feed reads, and the ack write gate."""

from datetime import UTC, date, datetime
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
                          severity="info", findings_json="{}", generated_at=datetime(2026, 7, 12, 1, 0, tzinfo=UTC)))
        s.add(SystemAudit(kind="breach", period_from=date(2026, 7, 8), period_to=date(2026, 7, 8),
                          breach_key="cap:2026-07-08", severity="alert", findings_json="{}",
                          generated_at=datetime(2026, 7, 8, 1, 0, tzinfo=UTC)))
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


def test_one_corrupt_findings_row_never_500s_the_lists(tmp_path: Path):
    """Per-row degrade (the cockpit posture): one corrupt ``findings_json`` row is
    SKIPPED -- reports and breaches still serve every parseable row, never a 500."""
    client, engine = _app(tmp_path)
    _seed(engine)
    with Session(engine) as s:
        s.add(SystemAudit(kind="weekly", period_from=date(2026, 7, 13),
                          period_to=date(2026, 7, 19), severity="info",
                          findings_json="{not json",
                          generated_at=datetime(2026, 7, 19, 1, 0, tzinfo=UTC)))
        s.add(SystemAudit(kind="breach", period_from=date(2026, 7, 9),
                          period_to=date(2026, 7, 9), breach_key="cap:2026-07-09",
                          severity="alert", findings_json="{not json",
                          generated_at=datetime(2026, 7, 9, 1, 0, tzinfo=UTC)))
        s.commit()
    reports = client.get("/api/audit/reports")
    breaches = client.get("/api/audit/breaches")
    assert reports.status_code == 200 and breaches.status_code == 200
    assert [r["period_to"] for r in reports.json()] == ["2026-07-12"]  # good row only
    assert [b["breach_key"] for b in breaches.json()] == ["cap:2026-07-08"]


def test_ack_requires_header_and_sets_flag(tmp_path: Path):
    client, engine = _app(tmp_path)
    bid = _seed(engine)
    assert client.post(f"/api/audit/{bid}/ack").status_code == 403        # no header
    ok = client.post(f"/api/audit/{bid}/ack", headers=_HDR)
    assert ok.status_code == 200 and ok.json()["acknowledged"] is True
    assert client.post("/api/audit/9999/ack", headers=_HDR).status_code == 404

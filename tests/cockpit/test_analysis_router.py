"""Analysis router (split from test_api.py): POST/GET /api/analysis and the
chart/PDF byte proxies."""

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from swing_screener.cockpit.routers.analysis import _pdf_filename, _worker_label
from swing_screener.db.models import (
    AnalysisRequest,
)
from swing_screener.storage import blob
from tests.cockpit.conftest import (
    _AZURE_URL,
    _HDR,
    _analysis_row,
    _client_and_engine,
    _signal_row,
)

# ---- deep analysis: POST/GET /api/analysis + the chart/PDF byte proxies ----

# 1x1 transparent PNG -- valid bytes (same fixture as tests/test_storage_blob.py).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PDF = b"%PDF-1.4 fake report bytes"

ANALYSIS_ROW_KEYS = {"id", "ticker", "status", "stalled", "requested_at", "started_at",
                     "finished_at", "summary", "error", "has_pdf", "chart_count"}


def test_request_analysis_requires_the_cockpit_header(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    assert client.post("/api/analysis", json={"ticker": "AMD"}).status_code == 403
    with Session(engine) as s:
        assert s.query(AnalysisRequest).count() == 0  # nothing queued


def test_request_analysis_uppercases_the_ticker(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    r = client.post("/api/analysis", json={"ticker": "  nvda "}, headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"id", "ticker", "status", "requested_at"}
    assert body["ticker"] == "NVDA"  # stripped + uppercased
    assert body["status"] == "queued"
    with Session(engine) as s:
        row = s.get(AnalysisRequest, body["id"])
        assert row is not None and row.ticker == "NVDA" and row.status == "queued"
        assert row.recipient == ""  # repo default: the worker resolves at send time


@pytest.mark.parametrize("bad", [
    {"ticker": ""},         # ticker required
    {"ticker": "   "},      # whitespace-only is still missing
    {"ticker": "A" * 40},   # over String(16)
    {"ticker": "ＡＭＤ"},    # fullwidth look-alike: tickers are ASCII by construction
    {},                     # missing entirely
])
def test_request_analysis_validation(tmp_path: Path, bad: dict[str, object]) -> None:
    client, engine = _client_and_engine(tmp_path)
    assert client.post("/api/analysis", json=bad, headers=_HDR).status_code == 422
    with Session(engine) as s:
        assert s.query(AnalysisRequest).count() == 0


def test_request_analysis_stamps_utc_requested_at(tmp_path: Path) -> None:
    """The SERVER stamps requested_at in aware UTC -- the worker claims and requeues
    by UTC comparison, so a naive-local stamp (the retired Streamlit form's bug
    surface) would mis-age requests by the zone offset."""
    client, _engine = _client_and_engine(tmp_path)
    before = datetime.now(UTC)
    r = client.post("/api/analysis", json={"ticker": "AMD"}, headers=_HDR)
    after = datetime.now(UTC)
    assert r.status_code == 200
    stamped = datetime.fromisoformat(r.json()["requested_at"])
    assert stamped.tzinfo is not None  # aware, never naive local
    assert before <= stamped <= after


def test_analysis_list_shape_order_and_manual_worker(tmp_path: Path) -> None:
    """GET /api/analysis: newest requested first, the full per-row wire shape,
    has_pdf/chart_count derived without touching a resolver, and worker 'manual'
    for a local DB (no scheduled drain -- requests wait for a manual
    `python -m swing_screener.notify.ondemand` run)."""
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(_analysis_row(
            ticker="OLD", requested_at=datetime(2026, 7, 9, 12, 0, tzinfo=UTC), status="done",
            started_at=datetime(2026, 7, 9, 12, 5, tzinfo=UTC),
            finished_at=datetime(2026, 7, 9, 12, 9, tzinfo=UTC), summary="looks fine",
            pdf_blob_key="20260709/OLD_report.pdf",
            chart_blob_keys="a.png, b.png,,"))  # split, strip, drop empties -> 2
        s.add(_analysis_row(ticker="NEW", requested_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC)))
        s.commit()
    r = client.get("/api/analysis")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"requests", "worker"}
    assert body["worker"] == "manual"
    assert [row["ticker"] for row in body["requests"]] == ["NEW", "OLD"]
    new, old = body["requests"]
    assert set(new) == ANALYSIS_ROW_KEYS and set(old) == ANALYSIS_ROW_KEYS
    assert new["status"] == "queued" and new["stalled"] is False
    assert new["has_pdf"] is False and new["chart_count"] == 0
    assert new["started_at"] is None and new["finished_at"] is None
    assert new["summary"] == "" and new["error"] is None
    assert old["has_pdf"] is True and old["chart_count"] == 2
    assert old["summary"] == "looks fine"
    # Naive DB values are stamped UTC on the wire ('+00:00'-suffixed): JS's
    # Date() parses naive ISO as LOCAL and would skew relative-time renders.
    assert old["requested_at"] == "2026-07-09T12:00:00+00:00"
    assert old["started_at"] == "2026-07-09T12:05:00+00:00"
    assert old["finished_at"] == "2026-07-09T12:09:00+00:00"


def test_worker_label_derives_cloud_vs_manual() -> None:
    """The worker field is URL-shaped (the _is_azure predicate), never
    connectivity-shaped: an unreachable Azure DB still HAS the cloud drain."""
    assert _worker_label(_AZURE_URL) == "cloud (*/15min)"
    assert _worker_label("sqlite:///local.db") == "manual"


def test_analysis_list_stalled_mirrors_the_worker_window(tmp_path: Path) -> None:
    """stalled = running AND started_at older than the worker's 30min requeue window
    (the next worker pass will requeue exactly these rows). Stored datetimes come
    back NAIVE (sqlite/mssql drop tzinfo) and are read as UTC -- the worker stamps
    UTC. A finished row is never stalled, however old its started_at."""
    client, engine = _client_and_engine(tmp_path)
    now = datetime.now(UTC).replace(tzinfo=None)  # naive UTC, as the DB returns it
    with Session(engine) as s:
        s.add(_analysis_row(ticker="STUCK", status="running",
                            started_at=now - timedelta(minutes=31)))
        s.add(_analysis_row(ticker="FRESH", status="running",
                            started_at=now - timedelta(minutes=5)))
        s.add(_analysis_row(ticker="QUEUED", status="queued"))
        s.add(_analysis_row(ticker="DONE", status="done",
                            started_at=now - timedelta(hours=2), finished_at=now))
        s.commit()
    rows = {r["ticker"]: r for r in client.get("/api/analysis").json()["requests"]}
    assert rows["STUCK"]["stalled"] is True
    assert rows["FRESH"]["stalled"] is False
    assert rows["QUEUED"]["stalled"] is False
    assert rows["DONE"]["stalled"] is False


def test_stale_after_locksteps_with_the_worker() -> None:
    """api._STALE_AFTER is RESTATED, not imported: notify.ondemand's module scope
    drags the pipeline + notify graph (pipeline.run, notify.analysis/pdf/transport)
    into the cockpit's import graph. This pin is the drift alarm; the heavy import
    lives HERE, test-only."""
    from swing_screener.cockpit import api
    from swing_screener.notify import ondemand
    assert api._STALE_AFTER == ondemand._STALE_AFTER


def test_analysis_list_limit_bounds(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        for i in range(3):
            s.add(_analysis_row(ticker=f"T{i}",
                                requested_at=datetime(2026, 7, 10, 12, i, tzinfo=UTC)))
        s.commit()
    assert client.get("/api/analysis?limit=0").status_code == 422
    assert client.get("/api/analysis?limit=201").status_code == 422
    body = client.get("/api/analysis?limit=2").json()
    assert [r["ticker"] for r in body["requests"]] == ["T2", "T1"]  # newest, capped


def test_analysis_chart_and_pdf_blob_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blob store configured: the proxies hand the STORED key (index-resolved
    server-side) to the resolver and stream its bytes with the right media type +
    Content-Disposition. Patches storage.blob's own download_bytes global --
    never imports azure."""
    monkeypatch.setenv("SWING_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net")
    seen: list[str] = []

    def fake_download(key: str) -> bytes:
        seen.append(key)
        return _PDF if key.endswith(".pdf") else _PNG

    monkeypatch.setattr(blob, "download_bytes", fake_download)
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        row = _analysis_row(
            status="done", pdf_blob_key="20260710/AMD_report.pdf",
            chart_blob_keys="20260710/AMD_1d.png,20260710/AMD_1wk.png")
        s.add(row)
        s.commit()
        rid = row.id
    r = client.get(f"/api/analysis/{rid}/chart/1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == _PNG
    assert seen == ["20260710/AMD_1wk.png"]  # the stored key, nothing client-supplied
    p = client.get(f"/api/analysis/{rid}/pdf")
    assert p.status_code == 200
    assert p.headers["content-type"] == "application/pdf"
    assert p.headers["content-disposition"] == 'attachment; filename="AMD_report.pdf"'
    assert p.content == _PDF
    assert seen[-1] == "20260710/AMD_report.pdf"


def test_analysis_chart_and_pdf_local_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No blob store: the stored values are local paths; the proxies serve their
    BYTES (FastAPI serves bytes, unlike st.image which also took a path)."""
    monkeypatch.delenv("SWING_BLOB_ACCOUNT_URL", raising=False)
    chart = tmp_path / "AMD_1d.png"
    chart.write_bytes(_PNG)
    pdf = tmp_path / "AMD_report.pdf"
    pdf.write_bytes(_PDF)
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        row = _analysis_row(status="done", pdf_blob_key=str(pdf),
                            chart_blob_keys=str(chart))
        s.add(row)
        s.commit()
        rid = row.id
    r = client.get(f"/api/analysis/{rid}/chart/0")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == _PNG
    p = client.get(f"/api/analysis/{rid}/pdf")
    assert p.status_code == 200
    assert p.headers["content-type"] == "application/pdf"
    assert p.headers["content-disposition"] == 'attachment; filename="AMD_report.pdf"'
    assert p.content == _PDF


def test_pdf_filename_survives_a_fullwidth_ticker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy row whose ticker rode in before the model's ASCII pin (fullwidth
    ＡＭＤ passes str.isalnum -- it is Unicode-aware) must still DOWNLOAD: a
    non-ASCII char reaching starlette's latin-1 header encoding is an unhandled
    500 inside the sanitizer's own threat model. The allowlist is ASCII-pinned,
    so the emptied name falls back to 'analysis'."""
    monkeypatch.delenv("SWING_BLOB_ACCOUNT_URL", raising=False)
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(_PDF)
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        row = _analysis_row(ticker="ＡＭＤ", status="done", pdf_blob_key=str(pdf))
        s.add(row)
        s.commit()
        rid = row.id
    p = client.get(f"/api/analysis/{rid}/pdf")
    assert p.status_code == 200  # never a 500
    assert (p.headers["content-disposition"]
            == 'attachment; filename="analysis_report.pdf"')
    assert p.content == _PDF
    # Unit pins: per-char the filter is ascii AND (alnum or ._-), not either.
    assert _pdf_filename("ＡＭＤ2") == "2_report.pdf"
    assert _pdf_filename('A"MD\r\n') == "AMD_report.pdf"


def test_analysis_asset_404_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 404 posture, every branch: unknown id; a row with no charts / no pdf;
    an out-of-range index and a NEGATIVE index (never Python's end-relative
    indexing); an aged-out blob (the resolver's download fails -> None). Most
    requests eventually age out of the store -- 404 is a normal state here."""
    monkeypatch.setenv("SWING_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net")

    def gone(key: str) -> bytes:
        raise FileNotFoundError(key)  # aged-out: the resolver catches -> None

    monkeypatch.setattr(blob, "download_bytes", gone)
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        bare = _analysis_row(ticker="BARE")  # queued: no keys at all
        full = _analysis_row(ticker="FULL", status="done", pdf_blob_key="p.pdf",
                             chart_blob_keys="a.png")
        s.add_all([bare, full])
        s.commit()
        bare_id, full_id = bare.id, full.id
    assert client.get("/api/analysis/99999/chart/0").status_code == 404
    assert client.get("/api/analysis/99999/pdf").status_code == 404
    assert client.get(f"/api/analysis/{bare_id}/chart/0").status_code == 404
    assert client.get(f"/api/analysis/{bare_id}/pdf").status_code == 404
    assert client.get(f"/api/analysis/{full_id}/chart/1").status_code == 404
    assert client.get(f"/api/analysis/{full_id}/chart/-1").status_code == 404
    assert client.get(f"/api/analysis/{full_id}/chart/0").status_code == 404
    assert client.get(f"/api/analysis/{full_id}/pdf").status_code == 404


def test_chart_index_traversal_shapes_never_reach_a_resolver(tmp_path: Path) -> None:
    """A path-shaped index never reaches a resolver -- pinned at BOTH gates it can
    die at: an encoded-slash segment (..%2F..) decodes to a slash and fails ROUTE
    matching (404, measured -- the plan guessed 422), while a plain non-int
    segment is the int path param's 422. Either way it is rejected before any DB
    or file access; the proxies only ever pass DB-stored keys to the resolvers,
    so there is no client-controlled read path."""
    client, _engine = _client_and_engine(tmp_path)
    assert client.get("/api/analysis/1/chart/..%2F..").status_code == 404
    assert client.get("/api/analysis/1/chart/0abc").status_code == 422
    assert client.get("/api/signals/..%2F../chart").status_code == 404
    assert client.get("/api/signals/abc/chart").status_code == 422


def test_signal_chart_local_branch_and_404s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/signals/{id}/chart serves Signal.chart_path bytes; 404 on an
    unknown id, a chartless signal (MOST signals -- the normal case, not an
    error), or a missing local file."""
    monkeypatch.delenv("SWING_BLOB_ACCOUNT_URL", raising=False)
    chart = tmp_path / "sig.png"
    chart.write_bytes(_PNG)
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        with_chart = _signal_row(chart_path=str(chart))
        chartless = _signal_row(ticker="NVDA")  # chart_path None
        missing = _signal_row(ticker="MSFT", chart_path=str(tmp_path / "gone.png"))
        s.add_all([with_chart, chartless, missing])
        s.commit()
        ok_id, none_id, gone_id = with_chart.id, chartless.id, missing.id
    r = client.get(f"/api/signals/{ok_id}/chart")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == _PNG
    assert client.get(f"/api/signals/{none_id}/chart").status_code == 404
    assert client.get(f"/api/signals/{gone_id}/chart").status_code == 404
    assert client.get("/api/signals/99999/chart").status_code == 404


def test_signal_chart_blob_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWING_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net")
    seen: list[str] = []

    def fake_download(key: str) -> bytes:
        seen.append(key)
        return _PNG

    monkeypatch.setattr(blob, "download_bytes", fake_download)
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        sig = _signal_row(chart_path="20260710/AMD_1d_20260710.png")
        s.add(sig)
        s.commit()
        sig_id = sig.id
    r = client.get(f"/api/signals/{sig_id}/chart")
    assert r.status_code == 200 and r.content == _PNG
    assert seen == ["20260710/AMD_1d_20260710.png"]  # the stored key, verbatim



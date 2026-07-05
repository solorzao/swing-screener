"""Cockpit API contract: health never lies or leaks, heartbeats and stats are typed,
and a dead database is a friendly 503 -- never a traceback, never the URL."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import _down_summary, connection_label, create_app
from swing_screener.db.models import EmailLog, PaperTrade
from swing_screener.db.session import get_engine

STAT_KEYS = {"value", "n", "n_clusters", "ci_low", "ci_high", "cost_level",
             "corpus_id", "facet", "unit", "thin_clusters"}


def _trade(ticker: str, r: float, *, play_type: str = "reversal",
           strength: str | None = "confirmed") -> PaperTrade:
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account="research", play_type=play_type, strength=strength,
        fill_status="filled", stop=95.0, target=110.0, risk=5.0, status="closed",
        realized_r=r,
    )


def _db_url(tmp_path: Path) -> str:
    # as_posix(): backslashes in a sqlite URL are asking for escape trouble.
    return f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"


def _client(tmp_path: Path) -> TestClient:
    url = _db_url(tmp_path)
    get_engine(url)  # seed the file + schema; the app builds its OWN engine from the URL
    return TestClient(create_app(url, edge_dir=tmp_path))


def test_health_reports_connection_label_without_credentials(tmp_path: Path) -> None:
    r = _client(tmp_path).get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["label"] == "Local SQLite · cockpit.db"
    # The ported chip guarantee: label never echoes the URL, the directory, or any
    # credential material (design doc: "never prints the connection string").
    assert _db_url(tmp_path) not in body["label"]
    assert tmp_path.as_posix() not in body["label"]
    azure = connection_label(
        "mssql+pyodbc://user:s3cret@srv.database.windows.net/swing?driver=ODBC+18"
    )
    assert azure == "Azure SQL · swing"
    assert "s3cret" not in azure
    assert "srv.database.windows.net" not in azure
    assert connection_label("total nonsense ://") == "Database"


def test_heartbeats_endpoint_returns_states(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    engine = get_engine(url)
    now = datetime.now(UTC)
    with Session(engine) as s:
        s.add(EmailLog(sent_at=now - timedelta(hours=2), kind="daily",
                       subject="x", run_date=now.date()))
        s.commit()
    r = TestClient(create_app(url, edge_dir=tmp_path)).get("/api/heartbeats")
    assert r.status_code == 200
    beats = {b["name"]: b for b in r.json()}
    for b in beats.values():
        assert set(b) == {"name", "state", "last", "period_s", "grace_s", "detail"}
        # `last` is ISO-8601 or null -- fromisoformat is the round-trip proof.
        assert b["last"] is None or datetime.fromisoformat(b["last"])
    # The Phase-2 GH pollers ship as explicit UNKNOWN rows, never silently absent.
    for name in ("GH · optimizer", "GH · reflection", "GH · CI"):
        assert beats[name]["state"] == "unknown"
    assert beats["daily digest"]["state"] == "up"
    assert beats["daily digest"]["last"] is not None


def test_cohort_stats_are_stat_objects(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_trade(t, r) for t, r in
                   [("AAA", 1.0), ("BBB", -0.5), ("CCC", 0.3), ("DDD", 0.8)]])
        s.commit()
    r = TestClient(create_app(url, edge_dir=tmp_path)).get("/api/stats/cohorts")
    assert r.status_code == 200
    cohorts = r.json()["cohorts"]
    assert cohorts, "seeded closed research trades must yield cohorts"
    for c in cohorts:
        assert set(c) == {"key", "strength", "stat"}
        # Rule 1: the full 10-key Stat shape, no bare numbers on the wire.
        assert set(c["stat"]) == STAT_KEYS
        assert c["stat"]["cost_level"] is None  # honest unknown, not a guess
        assert c["stat"]["corpus_id"] is None
        assert c["stat"]["facet"] == "research"
    rows = {(c["key"], c["strength"]): c["stat"] for c in cohorts}
    # Per-play_type aggregate (strength null) AND the (play_type, strength) split.
    assert ("reversal", None) in rows
    assert ("reversal", "confirmed") in rows
    assert rows[("reversal", None)]["n"] == 4
    assert rows[("reversal", "confirmed")]["n"] == 4


def test_down_summary_drops_the_exception_message() -> None:
    """The leak guard's wire form is the exception CLASS alone: driver messages can
    embed the DSN (host, password, file path), so none of the message may survive."""
    summary = _down_summary(Exception("Server=secret-host;PWD=hunter2"))
    assert summary == "database unreachable (Exception)"
    assert "secret-host" not in summary
    assert "hunter2" not in summary


def test_cohorts_keep_null_strength_aggregate_and_string_None_split_distinct(
    tmp_path: Path,
) -> None:
    """A null-strength trade yields TWO different rows: the per-play_type aggregate
    (``strength: null``) and the ``"None"`` string-key cohort from ``breakdown``'s
    str() coercion. Conflating them would silently pool unlike cohorts."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_trade(t, r) for t, r in
                   [("AAA", 1.0), ("BBB", -0.5), ("CCC", 0.3), ("DDD", 0.8)]])
        s.add(_trade("EEE", 0.5, strength=None))
        s.add(_trade("FFF", 0.2, play_type="continuation"))
        s.commit()
    r = TestClient(create_app(url, edge_dir=tmp_path)).get("/api/stats/cohorts")
    assert r.status_code == 200
    cohorts = r.json()["cohorts"]
    rows = {(c["key"], c["strength"]): c["stat"] for c in cohorts}
    assert ("reversal", None) in rows and ("reversal", "None") in rows
    assert rows[("reversal", None)]["n"] == 5  # aggregate pools ALL reversal trades
    assert rows[("reversal", "None")]["n"] == 1  # the null-strength cohort alone
    # Endpoint docstring's ordering contract: play_type-major, aggregate first,
    # then the per-strength split alphabetically.
    assert [(c["key"], c["strength"]) for c in cohorts] == [
        ("continuation", None), ("continuation", "confirmed"),
        ("reversal", None), ("reversal", "None"), ("reversal", "confirmed")]


def test_stale_schema_mid_request_is_a_friendly_503_not_a_500(tmp_path: Path) -> None:
    """A local.db from an OLDER schema passes the SELECT-1 probe, then blows up
    INSIDE the endpoint query (missing column -> OperationalError). That mid-request
    failure must reach the wire in the same friendly-503 posture as a dead DB --
    class name only -- never FastAPI's generic 500. Repro: rename a column the
    cohorts SELECT needs (create_all only creates missing TABLES, so the app's own
    engine leaves the mutilated table exactly as a stale local.db would be)."""
    url = _db_url(tmp_path)
    engine = get_engine(url)  # seeds the full current schema first
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE paper_trades RENAME COLUMN conviction_tier TO legacy"))
    client = TestClient(create_app(url, edge_dir=tmp_path), raise_server_exceptions=False)
    r = client.get("/api/stats/cohorts")
    assert r.status_code == 503, f"stale schema must 503, got {r.status_code}"
    # The exact wire form: 'error' (mid-request), distinct from 'unreachable' (probe).
    assert r.json() == {"detail": "database error (OperationalError)"}
    # Leak posture, same as _down_summary: the driver message embeds the SQL and
    # sqlite may name the file -- none of it, and no traceback, may survive.
    assert "cockpit.db" not in r.text
    assert tmp_path.as_posix() not in r.text
    assert "SELECT" not in r.text
    assert "Traceback" not in r.text


def test_db_down_is_a_friendly_503(tmp_path: Path) -> None:
    # Z: is not a mapped drive on this box, so sqlite genuinely cannot open the file.
    url = "sqlite:///Z:/definitely/nope/x.db"
    # raise_server_exceptions=False: an unhandled error would surface as a real 500
    # response and fail the assertions below -- the constraint under test.
    client = TestClient(create_app(url, edge_dir=tmp_path), raise_server_exceptions=False)

    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body["label"] == "Local SQLite · x.db"
    # The exact wire form: sqlalchemy raises OperationalError for an unopenable
    # sqlite file, and the leak guard forwards ONLY that class name.
    assert body["error"] == "database unreachable (OperationalError)"
    assert url not in body["error"]

    for path in ("/api/heartbeats", "/api/stats/cohorts"):
        r = client.get(path)
        assert r.status_code == 503, f"{path} must 503, got {r.status_code}"
        assert r.json()["detail"] == "database unreachable (OperationalError)"

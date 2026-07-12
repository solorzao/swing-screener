"""Cockpit API contract: health never lies or leaks, heartbeats and stats are typed,
and a dead database is a friendly 503 -- never a traceback, never the URL."""

import base64
from collections.abc import Callable, MutableMapping
from dataclasses import fields
from datetime import UTC, date, datetime, timedelta
import itertools
from pathlib import Path
import sys
from typing import Any

import anyio
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from swing_screener.analytics.calibration import _CLUSTER_FLOOR, MIN_LEADERBOARD_N
from swing_screener.analytics.performance import COST_STAMPED_FROM, SCORE_STAMPED_FROM
from swing_screener.cockpit.api import (
    _ACCOUNT_FOR_MODE,
    _LOGIN_TTL_S,
    _STATE_RANK,
    _change_token,
    _down_summary,
    _LoginFlight,
    _override_note,
    _pdf_filename,
    _safe_change_token,
    _worker_label,
    TradeCreate,
    connection_label,
    create_app,
)
from swing_screener.cockpit.settlement import STATES
from swing_screener.config import StrategyConfig
from swing_screener.db.models import (
    AnalysisRequest,
    AnalystCall,
    EmailLog,
    ExecutionLog,
    ExitEvent,
    PaperTrade,
    Signal,
    Trade,
)
from swing_screener.settings import _EXECUTION_MODES
from swing_screener.storage import blob
from swing_screener.db.repo import save_reversal_funnel
from swing_screener.db.session import get_engine
from swing_screener.pipeline.broker import BrokerOrderSpec, FakeBroker
from swing_screener.pipeline.proposed import (
    ProposedVariant,
    load_proposed_for,
    proposed_to_json,
    store_filename,
)
from swing_screener.pipeline.registry import Experiment

STAT_KEYS = {"value", "n", "n_clusters", "ci_low", "ci_high", "cost_level",
             "corpus_id", "facet", "unit", "thin_clusters"}

# The SettlementCard wire form -- a closed set, like STAT_KEYS.
CARD_KEYS = {"name", "kind", "play_type", "state", "n_accrued", "n_needed", "eta",
             "stopping_rule", "registered_sha", "registered_at", "mde_r", "book",
             "control", "delta", "upper_bound_type", "spark", "decision"}


def _trade(ticker: str, r: float, *, play_type: str = "reversal",
           strength: str | None = "confirmed", would_surface: bool | None = None,
           exit_date: date | None = None) -> PaperTrade:
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account="research", play_type=play_type, strength=strength,
        would_surface=would_surface, fill_status="filled", stop=95.0, target=110.0,
        risk=5.0, status="closed", realized_r=r, exit_date=exit_date,
    )


def _book_trade(ticker: str, r: float, *, arm: str = "baseline", variant: str = "default",
                trigger_ts: datetime | None = None, exit_date: date | None = None,
                would_surface: bool | None = None) -> PaperTrade:
    """A closed research-grid row for the forward-books loader: arm/variant/trigger_ts
    are the loader's filter keys and (for arms) the pair identity."""
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account="research", play_type="reversal", strength="confirmed", arm=arm,
        variant=variant, trigger_ts=trigger_ts, would_surface=would_surface,
        fill_status="filled", stop=95.0, target=110.0, risk=5.0, status="closed",
        realized_r=r, exit_date=exit_date,
    )


def _experiment(name: str, *, kind: str, status: str = "active",
                decision: str | None = None) -> Experiment:
    return Experiment(
        name=name, kind=kind, play_type="reversal",
        control="baseline" if kind == "arm" else "default",
        hypothesis="h", stopping_rule="rule text verbatim", mde_r=0.10,
        target_ci_halfwidth_r=0.15, registered_at="2026-07-10",
        registered_sha="abc123", doc_ref="docs/x.md", provenance="test",
        status=status, decided_at="2026-07-01" if decision else None, decision=decision,
    )


def _write_registry(edge_dir: Path, experiments: list[Experiment]) -> None:
    payload = "[" + ",".join(e.as_json() for e in experiments) + "]"
    (edge_dir / "experiments.json").write_text(payload, encoding="utf-8")


def _db_url(tmp_path: Path) -> str:
    # as_posix(): backslashes in a sqlite URL are asking for escape trouble.
    return f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"


def _client(tmp_path: Path) -> TestClient:
    url = _db_url(tmp_path)
    get_engine(url)  # seed the file + schema; the app builds its OWN engine from the URL
    return TestClient(create_app(url, edge_dir=tmp_path))


class _FakeProc:
    """Stands in for subprocess.Popen behind the spawner seam: poll() is the whole
    contract the endpoint reads."""
    def __init__(self) -> None:
        self.exited: int | None = None

    def poll(self) -> int | None:
        return self.exited


_HDR = {"X-Cockpit": "1"}
_AZURE_URL = "mssql+pyodbc://@srv.database.windows.net/swing?driver=ODBC+Driver+18"


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


def test_heartbeats_wire_the_gh_poller_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SWING_GH_TOKEN + SWING_GH_REPO (both set, read once at create_app time) turn
    the GH placeholder rows into real beats via cockpit.gh's latest_workflow_run,
    called once per workflow file with the configured repo + token."""
    calls: list[tuple[str, str, str]] = []

    def fake_latest(repo: str, workflow: str, token: str) -> tuple[datetime, str] | None:
        calls.append((repo, workflow, token))
        return (datetime.now(UTC) - timedelta(days=1), "success")

    monkeypatch.setattr("swing_screener.cockpit.api.latest_workflow_run", fake_latest)
    monkeypatch.setenv("SWING_GH_TOKEN", "tok")
    monkeypatch.setenv("SWING_GH_REPO", "oliver/swing-screener")
    beats = {b["name"]: b for b in _client(tmp_path).get("/api/heartbeats").json()}
    for name in ("GH · optimizer", "GH · reflection", "GH · CI"):
        assert beats[name]["state"] == "up"
        assert beats[name]["period_s"] == 7 * 86400
        assert beats[name]["last"] is not None
    assert sorted(calls) == [
        ("oliver/swing-screener", "ci.yml", "tok"),
        ("oliver/swing-screener", "optimize.yml", "tok"),
        ("oliver/swing-screener", "reflect.yml", "tok"),
    ]


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


def test_cohorts_accepts_facet_param(tmp_path: Path) -> None:
    """``?facet=gold`` aggregates only would_surface-TRUTHY rows (None and False are
    both excluded -- reflect.py's forward_gold slice); the default facet stays the
    full research book, every Stat echoes the facet it was computed under, and an
    unknown facet is FastAPI's 422, never a silent fallback."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([
            _trade("AAA", 1.0, would_surface=True),
            _trade("BBB", 0.5, would_surface=True),
            _trade("CCC", -1.0, would_surface=False),
            _trade("DDD", -0.5),  # would_surface None: a legacy/replay row, never gold
        ])
        s.commit()
    client = TestClient(create_app(url, edge_dir=tmp_path))

    research = client.get("/api/stats/cohorts").json()["cohorts"]
    by_key = {(c["key"], c["strength"]): c["stat"] for c in research}
    assert by_key[("reversal", None)]["n"] == 4  # the default book is unchanged
    assert all(c["stat"]["facet"] == "research" for c in research)

    gold = client.get("/api/stats/cohorts?facet=gold").json()["cohorts"]
    by_key = {(c["key"], c["strength"]): c["stat"] for c in gold}
    assert by_key[("reversal", None)]["n"] == 2  # AAA + BBB only
    assert by_key[("reversal", None)]["value"] == pytest.approx(0.75)
    assert all(c["stat"]["facet"] == "gold" for c in gold)
    assert all(set(c["stat"]) == STAT_KEYS for c in gold)

    assert client.get("/api/stats/cohorts?facet=bogus").status_code == 422


def test_cohort_stats_carry_cost_level_after_cutoff(tmp_path: Path) -> None:
    """``cost_level`` derives from the cutoff rule per cohort subset: ``"0.05"`` iff
    EVERY closed trade provably exited on/after COST_STAMPED_FROM; a single
    pre-cutoff exit poisons the whole cohort back to null. Dates derive from the
    constant itself so an epoch change cannot silently invert this test."""
    post_cutoff = COST_STAMPED_FROM + timedelta(days=1)
    pre_cutoff = COST_STAMPED_FROM - timedelta(days=1)
    url = _db_url(tmp_path)
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_trade(t, r, exit_date=post_cutoff) for t, r in
                   [("AAA", 1.0), ("BBB", -0.5), ("CCC", 0.3), ("DDD", 0.8)]])
        s.commit()
    client = TestClient(create_app(url, edge_dir=tmp_path))
    cohorts = client.get("/api/stats/cohorts").json()["cohorts"]
    assert cohorts, "seeded closed research trades must yield cohorts"
    assert all(c["stat"]["cost_level"] == "0.05" for c in cohorts)

    with Session(engine) as s:
        s.add(_trade("EEE", 0.2, exit_date=pre_cutoff))  # pre-cutoff: gross R
        s.commit()
    cohorts = client.get("/api/stats/cohorts").json()["cohorts"]
    assert all(c["stat"]["cost_level"] is None for c in cohorts)


def test_forward_books_shape(tmp_path: Path) -> None:
    """``{"cards": [...]}``: every card carries the full SettlementCard field set,
    every numeric travels as a 10-key Stat dict, ``spark`` is ``[iso_date, float]``
    pairs, and cards order awaiting-decision first, then accruing, then retired --
    regardless of registry file order."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    exit_d = date(2026, 7, 3)
    trades: list[PaperTrade] = []
    # a_settle: 24 pairs on 10 tickers with tight ~0.5R deltas -> settled-awaiting-decision.
    # Mixed would_surface (10 gold pairs, 14 unstamped) so the gold facet is a real subset.
    for i in range(24):
        ts = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(hours=i)
        ticker = f"T{i % 10:02d}"
        jitter = ((i % 3) - 1) * 0.01
        gold_row = True if i < 10 else None
        trades.append(_book_trade(ticker, 0.0, trigger_ts=ts, exit_date=exit_d,
                                  would_surface=gold_row))
        trades.append(_book_trade(ticker, 0.5 + jitter, arm="a_settle", trigger_ts=ts,
                                  exit_date=exit_d, would_surface=gold_row))
    # rev_small: a 3-ticker variant book vs the default control -> accruing (n < 20).
    for i, r in enumerate([0.6, -0.2, 0.3]):
        trades.append(_book_trade(f"V{i:02d}", r, variant="rev_small", exit_date=exit_d))
    with Session(engine) as s:
        s.add_all(trades)
        s.commit()
    # Registry order is deliberately scrambled: the endpoint must re-order by state.
    _write_registry(tmp_path, [
        _experiment("a_retired", kind="arm", status="retired", decision="falsified"),
        _experiment("rev_small", kind="variant"),
        _experiment("a_settle", kind="arm"),
    ])
    client = TestClient(create_app(url, edge_dir=tmp_path))

    r = client.get("/api/forward-books")
    assert r.status_code == 200
    cards = r.json()["cards"]
    assert [(c["name"], c["state"]) for c in cards] == [
        ("a_settle", "settled-awaiting-decision"),
        ("rev_small", "accruing"),
        ("a_retired", "retired"),
    ]
    for c in cards:
        assert set(c) == CARD_KEYS
        for stat_key in ("book", "control", "delta"):
            assert set(c[stat_key]) == STAT_KEYS
        for point in c["spark"]:
            iso, value = point
            assert date.fromisoformat(iso)
            assert isinstance(value, int | float)
    settle = cards[0]
    assert settle["stopping_rule"] == "rule text verbatim"  # rendered verbatim
    assert settle["registered_sha"] == "abc123"
    assert settle["upper_bound_type"] == "iid"       # arm: the paired primitive
    assert settle["spark"], "a book with exit dates must carry sparkline points"
    assert cards[1]["upper_bound_type"] == "clustered"  # variant: two-sample bootstrap
    assert cards[2]["decision"] == "falsified"       # retired history stays legible
    assert client.get("/api/forward-books?facet=bogus").status_code == 422

    # The gold facet threads through to build_cards: only the would_surface-truthy
    # pairs accrue (10 of the 24), and the cards say which facet produced them.
    gold_cards = client.get("/api/forward-books?facet=gold").json()["cards"]
    gold = {c["name"]: c for c in gold_cards}
    assert 0 < gold["a_settle"]["n_accrued"] < settle["n_accrued"]
    assert gold["a_settle"]["delta"]["facet"] == "gold"


def test_forward_books_state_rank_locksteps_with_settlement() -> None:
    """Lockstep guard: a new settlement state must teach the wall ordering about
    itself before it can ship -- /api/forward-books must never 500 on a KeyError."""
    assert set(_STATE_RANK) == set(STATES)


def test_forward_books_empty_registry(tmp_path: Path) -> None:
    """No experiments.json is a normal setup state -- an empty wall, never an error."""
    r = _client(tmp_path).get("/api/forward-books")
    assert r.status_code == 200
    assert r.json() == {"cards": []}


def test_funnel_endpoint(tmp_path: Path) -> None:
    """``{"funnel": null}`` before the first digest records a snapshot; afterwards the
    latest run_date's row with ``overflow`` split back to a list (empties dropped)."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    client = TestClient(create_app(url, edge_dir=tmp_path))
    assert client.get("/api/funnel").status_code == 200
    assert client.get("/api/funnel").json() == {"funnel": None}

    with Session(engine) as s:
        save_reversal_funnel(
            s, run_date=date(2026, 7, 9), detected=12, confirmed=7, fresh=6,
            actionable=5, surfaced=5, overflow_tickers="", pool_n=20,
            confirmed_only=True, premium_only=False, already_ran_checked=False)
    body = client.get("/api/funnel").json()
    assert body["funnel"]["overflow"] == []  # "" splits to no tickers, not [""]

    with Session(engine) as s:
        save_reversal_funnel(
            s, run_date=date(2026, 7, 10), detected=14, confirmed=8, fresh=6,
            actionable=6, surfaced=5, overflow_tickers="NVDA,AMD", pool_n=20,
            confirmed_only=True, premium_only=False, already_ran_checked=True)
    assert client.get("/api/funnel").json() == {"funnel": {
        "run_date": "2026-07-10", "detected": 14, "confirmed": 8, "fresh": 6,
        "actionable": 6, "surfaced": 5, "overflow": ["NVDA", "AMD"], "pool_n": 20,
        "confirmed_only": True, "premium_only": False, "already_ran_checked": True,
    }}


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


def test_health_carries_the_azure_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The flag describes the URL, not reachability: the frontend gates the sign-in
    # button on it, and an az login can never fix a sqlite file.
    assert _client(tmp_path).get("/api/health").json()["azure"] is False
    # Poison the whole azure package (not just azure.identity: `from azure.identity
    # import ...` would still import the parent for real) so the probe's lazy AAD
    # import raises fast on boxes WITH the [azure] extra -- no credential chain, no
    # network, and sys.modules stays azure-free for tests/test_config_secrets.py's
    # never-imports-azure invariant. Health catches it: connected stays truthful.
    monkeypatch.setitem(sys.modules, "azure", None)
    # A dev box exporting SWING_DB_ACCESS_TOKEN would take the static-token branch
    # (poison never consulted) and really dial out -- close that side door too.
    monkeypatch.delenv("SWING_DB_ACCESS_TOKEN", raising=False)
    azure_client = TestClient(create_app(_AZURE_URL, edge_dir=tmp_path))
    body = azure_client.get("/api/health").json()
    assert body["azure"] is True
    # Still 200 and truthful even where pyodbc isn't installed (CI has no [azure]
    # extra): connectivity may be down, the flag must not care.
    assert body["label"] == "Azure SQL · swing"


def _counting_spawner(calls: list[int]) -> Callable[[], _FakeProc]:
    def spawner() -> _FakeProc:
        calls.append(1)
        return _FakeProc()

    return spawner


def test_azure_login_requires_the_cockpit_header(tmp_path: Path) -> None:
    # Any webpage can fire a simple POST at localhost; the custom header forces a
    # failing CORS preflight cross-origin. No header -> 403 and NOTHING spawns.
    calls: list[int] = []
    client = TestClient(create_app(
        _AZURE_URL, edge_dir=tmp_path, login_spawner=_counting_spawner(calls),
    ))
    assert client.post("/api/azure-login").status_code == 403
    assert calls == []


def test_azure_login_409s_on_a_local_database(tmp_path: Path) -> None:
    calls: list[int] = []
    client = TestClient(create_app(
        _db_url(tmp_path), edge_dir=tmp_path, login_spawner=_counting_spawner(calls),
    ))
    assert client.post("/api/azure-login", headers=_HDR).status_code == 409
    assert calls == []


def test_azure_login_is_single_flight_until_the_process_exits(tmp_path: Path) -> None:
    procs: list[_FakeProc] = []

    def spawner() -> _FakeProc:
        procs.append(_FakeProc())
        return procs[-1]

    client = TestClient(create_app(_AZURE_URL, edge_dir=tmp_path, login_spawner=spawner))
    assert client.post("/api/azure-login", headers=_HDR).json() == {"started": True}
    # In-flight: a double-click must not open a second browser tab.
    assert client.post("/api/azure-login", headers=_HDR).json() == {
        "started": False, "already_running": True}
    assert len(procs) == 1
    procs[0].exited = 1  # the browser dance ended (success or not -- health decides)
    assert client.post("/api/azure-login", headers=_HDR).json() == {"started": True}
    assert len(procs) == 2


def test_azure_login_reports_a_missing_cli(tmp_path: Path) -> None:
    client = TestClient(create_app(
        _AZURE_URL, edge_dir=tmp_path, login_spawner=lambda: None))
    assert client.post("/api/azure-login", headers=_HDR).json() == {
        "started": False, "error": "az-not-found"}


def test_login_flight_ttl_expires_a_wedged_process() -> None:
    # The TTL half of the liveness check: a wedged CLI that never exits stops
    # blocking re-spawn once it outlives _LOGIN_TTL_S. (The poll() half is covered
    # by the single-flight test above; no clock seam -- active() takes `now`.)
    flight = _LoginFlight(proc=_FakeProc(), started=1000.0)
    assert flight.active(1000.0 + _LOGIN_TTL_S - 0.1) is True
    assert flight.active(1000.0 + _LOGIN_TTL_S) is False


# --- /api/stats/performance (Streamlit Screener Performance parity) -----------------

PERF_KEYS = {"kpis", "leaderboard", "arms", "breakdowns", "equity_curve"}
BREAKDOWN_KEYS = {"timeframe", "rank", "score", "market_trend", "market_vol"}
KPI_KEYS = {"expectancy", "win_rate", "fill_rate", "profit_factor", "n_closed"}
LEADERBOARD_ROW_KEYS = {"variant", "stat", "win_rate", "fill_rate", "n_total", "flag"}
ARM_ROW_KEYS = {"arm", "stat", "n_pairs", "delta"}
BREAKDOWN_ROW_KEYS = {"key", "stat", "win_rate", "n_closed"}


def _perf_trade(
    ticker: str, r: float | None = None, *, play_type: str = "continuation",
    arm: str = "baseline", variant: str = "default", timeframe: str = "1d",
    rank: int = 1, score: float = 0.8, fill_status: str = "filled",
    status: str = "closed", opened_date: date | None = None,
    exit_date: date | None = None, trigger_ts: datetime | None = None,
    market_trend: str | None = None, market_vol: str | None = None,
    would_surface: bool | None = None,
) -> PaperTrade:
    """A research-grid row shaped for the performance endpoint: variant/arm/opened_date
    are the leaderboard's and window's filter keys, trigger_ts the arm-pair identity."""
    return PaperTrade(
        ticker=ticker, timeframe=timeframe, horizon="medium", signal_score=score,
        rank=rank, account="research", play_type=play_type, arm=arm, variant=variant,
        trigger_ts=trigger_ts, would_surface=would_surface, market_trend=market_trend,
        market_vol=market_vol, fill_status=fill_status, stop=95.0, target=110.0,
        risk=5.0, status=status, realized_r=r, opened_date=opened_date,
        exit_date=exit_date, hold_bars=3 if status == "closed" else None,
    )


def _seed(url: str, trades: list[PaperTrade]) -> None:
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all(trades)
        s.commit()


def test_performance_kpis_breakdowns_and_equity_curve(tmp_path: Path) -> None:
    """Port of dashboard test_performance_renders_kpis_and_altair_charts: the KPI strip,
    every breakdown, and the equity curve as one JSON shape -- every aggregate a 10-key
    Stat, and every key present even when its section is degenerate (single variant /
    single arm / regime unknown everywhere)."""
    url = _db_url(tmp_path)
    _seed(url, [
        _perf_trade("AMD", 2.0, rank=1, score=0.9, exit_date=date(2026, 1, 5)),
        _perf_trade("NVDA", -1.0, rank=7, score=0.8, exit_date=date(2026, 1, 6)),
        _perf_trade("MSFT", 1.5, rank=12, score=0.7, timeframe="1w",
                    exit_date=date(2026, 1, 7)),
    ])
    r = TestClient(create_app(url, edge_dir=tmp_path)).get("/api/stats/performance")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == PERF_KEYS

    kpis = body["kpis"]
    assert set(kpis) == KPI_KEYS
    assert set(kpis["expectancy"]) == STAT_KEYS
    assert kpis["expectancy"]["value"] == pytest.approx(2.5 / 3)
    assert kpis["expectancy"]["facet"] == "research"
    assert kpis["expectancy"]["corpus_id"] is None
    assert kpis["win_rate"] == pytest.approx(2 / 3)
    assert kpis["fill_rate"] == 1.0
    assert kpis["profit_factor"] == pytest.approx(3.5)
    assert kpis["n_closed"] == 3

    assert set(body["breakdowns"]) == BREAKDOWN_KEYS
    by_tf = {row["key"]: row for row in body["breakdowns"]["timeframe"]}
    assert set(by_tf) == {"1d", "1w"}
    assert set(by_tf["1d"]) == BREAKDOWN_ROW_KEYS
    assert set(by_tf["1d"]["stat"]) == STAT_KEYS
    assert by_tf["1d"]["n_closed"] == 2 and by_tf["1d"]["win_rate"] == 0.5
    # Rank buckets keep their natural order and INCLUDE empty buckets (page parity:
    # rank_bucket emits every label) -- only score omits empties.
    assert [row["key"] for row in body["breakdowns"]["rank"]] == ["1-5", "6-10", "11+"]
    assert [row["key"] for row in body["breakdowns"]["score"]] == ["0.70-0.80", "0.80-1.00"]
    assert body["breakdowns"]["market_trend"] == []  # regime unknown on every row

    assert body["equity_curve"] == [
        ["2026-01-05", 2.0], ["2026-01-06", 1.0], ["2026-01-07", 2.5]]
    # Degenerate sections still return their data -- the frontend decides rendering.
    assert [row["variant"] for row in body["leaderboard"]] == ["default"]
    assert [row["arm"] for row in body["arms"]] == ["baseline"]


def test_performance_leaderboard_ranks_trusted_above_thin(tmp_path: Path) -> None:
    """Port of dashboard test_leaderboard_ranks_trusted_above_thin_lucky_sample: the
    deep +0.5R variant (n=25) outranks the lone lucky +3R trade despite the lower
    expectancy, and the 1-ticker variant is flagged 'iid' (no clustered bound)."""
    url = _db_url(tmp_path)
    trades = [_perf_trade(f"T{i}", 0.5, variant="deep", exit_date=date(2026, 1, 5))
              for i in range(25)]
    trades.append(_perf_trade("LUCK", 3.0, variant="thinlucky",
                              exit_date=date(2026, 1, 5)))
    _seed(url, trades)
    board = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()["leaderboard"]
    assert all(set(row) == LEADERBOARD_ROW_KEYS for row in board)
    order = [row["variant"] for row in board]
    assert order.index("deep") < order.index("thinlucky")
    flags = {row["variant"]: row["flag"] for row in board}
    assert flags == {"deep": "ok", "thinlucky": "iid"}


def test_performance_leaderboard_flags_iid_fallback_for_thin_clusters(
    tmp_path: Path,
) -> None:
    """Port of dashboard test_leaderboard_flags_iid_fallback_for_thin_clusters: a
    single-ticker variant's bound is the IID fallback ('iid'); an 8-ticker variant
    clusters fine and reads 'thin' (n<20) -- the cluster counts ride on the Stat."""
    url = _db_url(tmp_path)
    trades = [_perf_trade(f"T{i}", 0.5, variant="broad", exit_date=date(2026, 1, 5))
              for i in range(8)]
    trades += [_perf_trade("SOLO", r, variant="narrow", exit_date=date(2026, 1, 6))
               for r in (1.0, 1.2)]
    _seed(url, trades)
    board = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()["leaderboard"]
    rows = {row["variant"]: row for row in board}
    assert rows["broad"]["stat"]["n_clusters"] == 8
    assert rows["narrow"]["stat"]["n_clusters"] == 1
    assert rows["narrow"]["flag"] == "iid" and rows["broad"]["flag"] == "thin"


def test_performance_leaderboard_surfaces_fill_rate_and_total(tmp_path: Path) -> None:
    """Port of dashboard test_variant_leaderboard_surfaces_fill_rate_and_total: a
    variant that 'wins' by rarely filling must show it where the ranking is read."""
    url = _db_url(tmp_path)
    trades = [_perf_trade("AMD", 2.0, variant="picky", exit_date=date(2026, 1, 5))]
    trades += [_perf_trade(f"N{i}", None, variant="picky", fill_status="pending",
                           status="open") for i in range(3)]
    trades += [_perf_trade("MSFT", r, exit_date=date(2026, 1, 6)) for r in (1.0, 0.5)]
    _seed(url, trades)
    board = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()["leaderboard"]
    rows = {row["variant"]: row for row in board}
    assert rows["picky"]["fill_rate"] == 0.25 and rows["picky"]["n_total"] == 4
    assert rows["default"]["fill_rate"] == 1.0 and rows["default"]["n_total"] == 2


def test_performance_arms_ab_with_paired_delta(tmp_path: Path) -> None:
    """Port of dashboard test_performance_shows_per_arm_ab_when_multiple_arms (+ the
    arm-table scenario): per-arm rows with the spec'd wire shape, KPIs pinned to the
    page's default arm-detail selection (BASELINE), and -- the deviation that IS the
    spec -- each non-baseline arm carries a paired delta Stat built like settlement's
    arm branch. The baseline row's delta is null (no self-delta), n_pairs 0."""
    url = _db_url(tmp_path)
    t0 = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    exit_d = COST_STAMPED_FROM + timedelta(days=1)  # post-cutoff: both sides net @0.05
    _seed(url, [
        _perf_trade("AMD", 1.0, trigger_ts=t0, exit_date=exit_d),
        _perf_trade("AMD", 1.6, arm="partial33_cond", trigger_ts=t0, exit_date=exit_d),
    ])
    body = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()
    arms = {row["arm"]: row for row in body["arms"]}
    assert set(arms) == {"baseline", "partial33_cond"}
    for row in arms.values():
        assert set(row) == ARM_ROW_KEYS
        assert set(row["stat"]) == STAT_KEYS
    assert arms["baseline"]["stat"]["value"] == pytest.approx(1.0)
    assert arms["baseline"]["delta"] is None and arms["baseline"]["n_pairs"] == 0
    partial = arms["partial33_cond"]
    assert partial["stat"]["value"] == pytest.approx(1.6)
    assert partial["n_pairs"] == 1
    delta = partial["delta"]
    assert set(delta) == STAT_KEYS
    assert delta["value"] == pytest.approx(0.6)
    assert delta["n"] == 1
    assert delta["ci_low"] == pytest.approx(0.6)  # 1 pair: interval collapses to point
    assert delta["ci_high"] == pytest.approx(0.6)
    assert delta["thin_clusters"] is True and delta["unit"] == "R"
    assert delta["cost_level"] == "0.05"  # cost_level_for over the POOLED book (both sides)
    # KPIs read the page's default arm-detail selection: the BASELINE arm.
    assert body["kpis"]["expectancy"]["value"] == pytest.approx(1.0)
    assert body["kpis"]["n_closed"] == 1


def test_performance_arm_pin_falls_back_alphabetically_without_baseline(
    tmp_path: Path,
) -> None:
    """A multi-arm book WITHOUT a baseline arm pins the KPI/breakdown subset to the
    first arm alphabetically -- the page's radio default (index 0 when BASELINE is
    absent). Deltas vs the missing baseline honestly carry zero pairs."""
    url = _db_url(tmp_path)
    _seed(url, [
        _perf_trade("AMD", 1.6, arm="partial33_cond", exit_date=date(2026, 1, 5)),
        _perf_trade("AMD", 0.9, arm="flip_only", exit_date=date(2026, 1, 5)),
    ])
    body = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()
    assert [row["arm"] for row in body["arms"]] == ["flip_only", "partial33_cond"]
    assert body["kpis"]["expectancy"]["value"] == pytest.approx(0.9)  # flip_only pins
    assert body["kpis"]["n_closed"] == 1
    assert all(row["n_pairs"] == 0 for row in body["arms"])  # no baseline: no pairs


def test_performance_play_type_filter_scopes_the_arm_ab(tmp_path: Path) -> None:
    """Port of dashboard test_performance_play_type_filter_scopes_the_arm_ab: the
    play_type param scopes EVERYTHING (the arms span both engines); unknown enum
    values are FastAPI's 422, never a silent 'all'."""
    url = _db_url(tmp_path)
    rows = [("continuation", "baseline", 1.0), ("continuation", "partial33_cond", 1.5),
            ("reversal", "baseline", -0.5), ("reversal", "partial33_cond", 0.5)]
    _seed(url, [_perf_trade("AMD", r, play_type=p, arm=a, exit_date=date(2026, 1, 5))
                for p, a, r in rows])
    client = TestClient(create_app(url, edge_dir=tmp_path))

    def _expectancy(query: str) -> float:
        body = client.get(f"/api/stats/performance{query}").json()
        value = body["kpis"]["expectancy"]["value"]
        assert isinstance(value, float)
        return value

    assert _expectancy("") == pytest.approx(0.25)  # baseline arm across both engines
    assert _expectancy("?play_type=reversal") == pytest.approx(-0.5)
    assert _expectancy("?play_type=continuation") == pytest.approx(1.0)
    assert client.get("/api/stats/performance?play_type=bogus").status_code == 422
    assert client.get("/api/stats/performance?window=45").status_code == 422


def test_performance_scopes_arms_to_default_variant(tmp_path: Path) -> None:
    """The arm A/B and every downstream KPI/breakdown slice to variant ==
    DEFAULT_VARIANT -- the A/B is only honest within one screen variant. The
    leaderboard (computed upstream of the scoping) still sees every variant."""
    url = _db_url(tmp_path)
    t0 = datetime(2026, 1, 2, tzinfo=UTC)
    t1 = datetime(2026, 1, 3, tzinfo=UTC)
    _seed(url, [
        _perf_trade("AMD", 1.0, trigger_ts=t0, exit_date=date(2026, 1, 5)),
        _perf_trade("AMD", 1.6, arm="partial33_cond", trigger_ts=t0,
                    exit_date=date(2026, 1, 5)),
        _perf_trade("NVDA", 5.0, variant="aggressive", trigger_ts=t1,
                    exit_date=date(2026, 1, 6)),
        _perf_trade("NVDA", 9.0, arm="partial33_cond", variant="aggressive",
                    trigger_ts=t1, exit_date=date(2026, 1, 6)),
    ])
    body = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()
    assert {row["variant"] for row in body["leaderboard"]} == {"default", "aggressive"}
    arms = {row["arm"]: row for row in body["arms"]}
    assert arms["baseline"]["stat"]["n"] == 1
    assert arms["baseline"]["stat"]["value"] == pytest.approx(1.0)  # never blends the 5.0
    assert arms["partial33_cond"]["n_pairs"] == 1  # the aggressive pair must not count
    assert arms["partial33_cond"]["delta"]["value"] == pytest.approx(0.6)  # not 2.3
    assert body["kpis"]["expectancy"]["value"] == pytest.approx(1.0)
    tf = {row["key"]: row for row in body["breakdowns"]["timeframe"]}
    assert tf["1d"]["n_closed"] == 1


def test_performance_window_cuts_after_arm_filter(tmp_path: Path) -> None:
    """The trailing window cuts the arm==BASELINE subset on opened_date: undated rows
    drop from windowed views but stay in 'all', non-baseline arms never enter the
    leaderboard at any window, and the window scopes ONLY the leaderboard (KPIs,
    breakdowns, and the equity curve are windowless, mirroring the page)."""
    url = _db_url(tmp_path)
    today = date.today()
    _seed(url, [
        _perf_trade("AAA", 1.0, opened_date=today - timedelta(days=10),
                    exit_date=today - timedelta(days=5)),
        _perf_trade("BBB", -1.0, opened_date=today - timedelta(days=200),
                    exit_date=today - timedelta(days=195)),
        _perf_trade("CCC", 0.5, exit_date=today - timedelta(days=3)),  # opened_date None
        _perf_trade("DDD", 2.0, arm="partial33_cond",
                    opened_date=today - timedelta(days=5),
                    exit_date=today - timedelta(days=2)),
    ])
    client = TestClient(create_app(url, edge_dir=tmp_path))
    everything = client.get("/api/stats/performance").json()
    board = {row["variant"]: row for row in everything["leaderboard"]}
    assert board["default"]["n_total"] == 3  # AAA+BBB+CCC; DDD is the wrong arm
    assert board["default"]["stat"]["n"] == 3

    windowed = client.get("/api/stats/performance?window=90").json()
    board90 = {row["variant"]: row for row in windowed["leaderboard"]}
    # BBB is beyond the window; CCC has no opened_date -> drops from windowed views.
    assert board90["default"]["n_total"] == 1
    assert board90["default"]["stat"]["value"] == pytest.approx(1.0)
    assert windowed["kpis"] == everything["kpis"]
    assert windowed["arms"] == everything["arms"]
    assert windowed["breakdowns"] == everything["breakdowns"]
    assert windowed["equity_curve"] == everything["equity_curve"]


def test_profit_factor_inf_is_null(tmp_path: Path) -> None:
    """An all-winner book's profit factor is float('inf') in summarize; JSON has no
    Infinity, so the wire form is null (the page rendered the same case as ∞)."""
    url = _db_url(tmp_path)
    _seed(url, [_perf_trade("AAA", 1.0, exit_date=date(2026, 1, 5)),
                _perf_trade("BBB", 0.5, exit_date=date(2026, 1, 6))])
    kpis = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()["kpis"]
    assert kpis["profit_factor"] is None
    assert kpis["win_rate"] == 1.0 and kpis["n_closed"] == 2


def test_performance_score_calibration_bands(tmp_path: Path) -> None:
    """Port of dashboard test_performance_renders_score_calibration, plus the two
    load-bearing shaping rules: bands with no closes are omitted (never spurious
    zeros), and the score cut goes through score_stamped -- a reversal row scored
    under the OLD definition (opened before the score-v2 cutoff) is excluded from
    the score breakdown while still counting everywhere else."""
    url = _db_url(tmp_path)
    stale = SCORE_STAMPED_FROM["reversal"] - timedelta(days=1)
    trades: list[PaperTrade] = []
    for i in range(4):
        trades.append(_perf_trade(f"L{i}", -1.0, score=0.45, exit_date=date(2026, 1, 5)))
        trades.append(_perf_trade(f"H{i}", 2.0, score=0.85, exit_date=date(2026, 1, 6)))
    trades.append(_perf_trade("OLD", 10.0, play_type="reversal", score=0.85,
                              opened_date=stale, exit_date=date(2026, 1, 7)))
    _seed(url, trades)
    body = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()
    score = {row["key"]: row for row in body["breakdowns"]["score"]}
    assert set(score) == {"0.00-0.50", "0.80-1.00"}  # empty bands omitted
    assert score["0.00-0.50"]["win_rate"] == 0.0 and score["0.00-0.50"]["n_closed"] == 4
    assert score["0.80-1.00"]["n_closed"] == 4  # OLD is excluded from the score cut...
    assert score["0.80-1.00"]["stat"]["value"] == pytest.approx(2.0)
    assert body["kpis"]["n_closed"] == 9  # ...but counts in every other aggregate


def test_performance_regime_breakdown_skips_unknown(tmp_path: Path) -> None:
    """Port of dashboard test_performance_renders_regime_breakdown: expectancy by SPY
    trend and vol regime; unknown-regime rows (None) never become a 'None' bucket."""
    url = _db_url(tmp_path)
    trades: list[PaperTrade] = []
    for i in range(3):
        trades.append(_perf_trade(f"B{i}", 1.5, market_trend="bull", market_vol="calm",
                                  exit_date=date(2026, 1, 5)))
        trades.append(_perf_trade(f"R{i}", -0.8, market_trend="bear", market_vol="high",
                                  exit_date=date(2026, 1, 6)))
    trades.append(_perf_trade("UNK", 0.1, exit_date=date(2026, 1, 7)))
    _seed(url, trades)
    body = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()
    trend = {row["key"]: row for row in body["breakdowns"]["market_trend"]}
    assert set(trend) == {"bull", "bear"}
    assert trend["bull"]["stat"]["value"] == pytest.approx(1.5)
    assert trend["bear"]["stat"]["value"] == pytest.approx(-0.8)
    assert trend["bull"]["n_closed"] == 3
    vol = {row["key"]: row for row in body["breakdowns"]["market_vol"]}
    assert set(vol) == {"calm", "high"}


def test_performance_facet_gold_filters_before_everything(tmp_path: Path) -> None:
    """?facet=gold applies facet_filter to the research trades BEFORE the play-type
    filter, leaderboard, and every downstream aggregate; every Stat echoes the facet
    it was computed under; an unknown facet is a 422."""
    url = _db_url(tmp_path)
    _seed(url, [
        _perf_trade("AAA", 1.0, would_surface=True, exit_date=date(2026, 1, 5)),
        _perf_trade("BBB", 0.5, would_surface=True, exit_date=date(2026, 1, 6)),
        _perf_trade("CCC", -1.0, would_surface=False, exit_date=date(2026, 1, 7)),
        _perf_trade("DDD", -0.5, exit_date=date(2026, 1, 8)),  # None: never gold
    ])
    client = TestClient(create_app(url, edge_dir=tmp_path))
    research = client.get("/api/stats/performance").json()
    assert research["kpis"]["n_closed"] == 4

    gold = client.get("/api/stats/performance?facet=gold").json()
    assert gold["kpis"]["n_closed"] == 2
    assert gold["kpis"]["expectancy"]["value"] == pytest.approx(0.75)
    assert gold["kpis"]["expectancy"]["facet"] == "gold"
    assert all(row["stat"]["facet"] == "gold" for row in gold["leaderboard"])
    assert all(row["stat"]["facet"] == "gold"
               for rows in gold["breakdowns"].values() for row in rows)
    assert len(gold["equity_curve"]) == 2
    assert client.get("/api/stats/performance?facet=bogus").status_code == 422


def test_performance_empty_book_returns_all_keys(tmp_path: Path) -> None:
    """Port of dashboard test_performance_empty_state_has_no_chart, reshaped: the API
    never hides sections -- every key is present with empty lists (or zero KPIs), and
    the frontend decides rendering. Rank keeps its fixed labels (page parity)."""
    body = _client(tmp_path).get("/api/stats/performance").json()
    assert set(body) == PERF_KEYS
    assert body["kpis"]["n_closed"] == 0
    assert body["kpis"]["profit_factor"] == 0.0  # zeros, not null: only inf -> null
    assert body["leaderboard"] == [] and body["arms"] == []
    assert body["equity_curve"] == []
    assert body["breakdowns"]["timeframe"] == []
    assert body["breakdowns"]["score"] == []  # every band empty -> all omitted
    assert body["breakdowns"]["market_trend"] == []
    assert body["breakdowns"]["market_vol"] == []
    assert [row["key"] for row in body["breakdowns"]["rank"]] == ["1-5", "6-10", "11+"]
    assert all(row["n_closed"] == 0 for row in body["breakdowns"]["rank"])


# --- /api/gate ------------------------------------------------------------------------

def _call(created: date, cost: float | None) -> AnalystCall:
    return AnalystCall(
        created_date=created, ticker="AMD", timeframe="1d", play_type="continuation",
        run_date=created, baseline_conviction="medium", final_conviction="high",
        nudge_reason="test", model="opus", est_cost_usd=cost,
    )


def test_gate_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """{"ready", "countdown", "execution_mode", "analyst_spend_today_usd"}: the report
    comes from pipeline.autonomy verbatim (missing verdicts sidecars read as not-ready,
    never an error), the mode from live settings at request time, and the spend sums
    est_cost_usd over TODAY's AnalystCall rows (NULL costs -- deterministic-path rows
    -- count 0; yesterday's spend never bleeds in)."""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "manual")
    url = _db_url(tmp_path)
    engine = get_engine(url)
    today = date.today()
    with Session(engine) as s:
        s.add_all([_call(today, 0.12), _call(today, 0.30), _call(today, None),
                   _call(today - timedelta(days=1), 5.0)])
        s.commit()
    r = TestClient(create_app(url, edge_dir=tmp_path)).get("/api/gate")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ready", "countdown", "execution_mode",
                         "analyst_spend_today_usd"}
    assert body["ready"] is False  # no verdicts sidecars in tmp_path, no scored calls
    # gate_countdown VERBATIM (line format pinned by tests/pipeline/
    # test_autonomy_countdown.py); denominators are the live floor constants.
    assert body["countdown"] == "\n".join(
        f"{pt}: 0/{MIN_LEADERBOARD_N} high, 0/{MIN_LEADERBOARD_N} low, "
        f"0/{_CLUSTER_FLOOR} tickers"
        for pt in ("continuation", "reversal"))
    assert body["execution_mode"] == "manual"
    assert body["analyst_spend_today_usd"] == pytest.approx(0.42)


# --- /api/events (SSE wake channel) ---------------------------------------------------

def test_change_token_moves_on_new_trade(tmp_path: Path) -> None:
    """The wake channel's whole contract lives in the token: stable while nothing
    changes, different after a write -- AND after a close. The SSE loop itself
    stays a thin compare-and-emit, so THIS is where the behavior is pinned."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    before = _change_token(engine, tmp_path)
    assert all(isinstance(v, str) for v in before.values())  # strings only on the wire
    assert _change_token(engine, tmp_path) == before  # no writes -> identical token
    with Session(engine) as s:
        s.add(_trade("AAA", 1.0))
        s.commit()
    after_insert = _change_token(engine, tmp_path)
    assert after_insert != before
    # A CLOSE is an UPDATE on paper_trades (status/exit_* set on the existing row;
    # no new id, no updated_at column), so max(PaperTrade.id) never moves -- the
    # ExitEvent that EVERY close path inserts (shadow.py / reconcile.py /
    # exitcheck.py) is the observable the token must watch.
    with Session(engine) as s:
        s.add(ExitEvent(created_date=date.today(), tier="base", reason="stop_hit"))
        s.commit()
    assert _change_token(engine, tmp_path) != after_insert


def test_change_token_survives_db_down(tmp_path: Path) -> None:
    """A dead DB must never kill the stream loop: the safe wrapper answers the
    sentinel token instead of raising -- recovery then reads as a change (the
    sentinel can never equal a real token). Both failure shapes are covered: the
    engine dies at query time, and the factory itself raises."""
    bad = create_engine("sqlite:///Z:/definitely/nope/x.db")  # unopenable at query time
    assert _safe_change_token(lambda: bad, tmp_path) == {"db": "down"}

    def boom() -> Engine:
        raise RuntimeError("engine factory failed")

    assert _safe_change_token(boom, tmp_path) == {"db": "down"}


def test_events_route_exists(tmp_path: Path) -> None:
    """GET /api/events answers 200 text/event-stream, and the FIRST event arrives on
    connect (``last`` starts None) -- the endpoint contract useEventWake builds on:
    an event means "refetch now", never evidence something changed.

    Deliberately NOT via TestClient: its transport runs the ASGI app to completion
    and buffers the whole body (testclient.py handle_request ->
    ``portal.call(self.app, ...)``), so an infinite SSE stream never yields headers
    -- ``client.stream()`` would hang, not stream. Instead this drives the raw ASGI
    app: capture ``http.response.start`` plus ONE body chunk (the always-on-connect
    event), then answer the next ``receive()`` with ``http.disconnect``, which
    sse-starlette's disconnect listener turns into a task-group cancel -- the app
    call returns cleanly with the rest of the infinite stream unconsumed.
    """
    url = _db_url(tmp_path)
    get_engine(url)  # seed the file + schema; the app builds its OWN engine
    app = create_app(url, edge_dir=tmp_path)

    async def _head() -> tuple[int, dict[str, str], bytes]:
        start: dict[str, Any] = {}
        chunks: list[bytes] = []
        got_body = anyio.Event()

        async def receive() -> dict[str, Any]:
            await got_body.wait()
            return {"type": "http.disconnect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body" and message.get("body"):
                chunks.append(bytes(message["body"]))
                got_body.set()

        scope: dict[str, Any] = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "path": "/api/events", "raw_path": b"/api/events", "root_path": "",
            "scheme": "http", "query_string": b"", "headers": [],
            "client": ("testclient", 50000), "server": ("testserver", 80),
        }
        with anyio.fail_after(10):  # a wedged stream FAILS the test, never hangs it
            await app(scope, receive, send)
        headers = {k.decode(): v.decode() for k, v in start["headers"]}
        return start["status"], headers, chunks[0]

    status, headers, first = anyio.run(_head)
    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert b"event: change" in first  # one event per (re)connect, always


# ---- trade write actions: POST /api/trades + POST /api/trades/{id}/close ----


def _trade_body(**over: object) -> dict[str, object]:
    """A valid log-trade body; override any field to break one rule at a time."""
    body: dict[str, object] = {"ticker": " amd ", "entry_price": 100.0, "size": 10.0,
                               "stop": 95.0, "target": 110.0}
    body.update(over)
    return body


def _signal_row(**over: object) -> Signal:
    """A prefill-source Signal: zone [96, 101], stop 95 (zone-R risk = 6), target 110."""
    row: dict[str, object] = dict(
        run_date=date(2026, 7, 10), ticker="AMD", timeframe="1d", horizon="medium",
        score=0.9, rank=1, trigger_close=100.0, atr=4.0, rsi=55.0,
        entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
    )
    row.update(over)
    return Signal(**row)


def _client_and_engine(tmp_path: Path) -> tuple[TestClient, Engine]:
    url = _db_url(tmp_path)
    engine = get_engine(url)  # seeds the file + schema; the app builds its own engine
    return TestClient(create_app(url, edge_dir=tmp_path)), engine


def test_log_trade_requires_the_cockpit_header(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    assert client.post("/api/trades", json=_trade_body()).status_code == 403
    with Session(engine) as s:
        assert s.query(Trade).count() == 0  # nothing persisted


@pytest.mark.parametrize("over", [
    {"ticker": ""},            # ticker required
    {"ticker": "   "},         # whitespace-only is still missing
    {"entry_price": 0.0},      # entry must be positive
    {"entry_price": -5.0},
    {"size": 0.0},             # size must be positive
    {"size": -1.0},
    {"stop": 100.0},           # stop must be BELOW entry (long)
    {"stop": 105.0},
    {"target": 100.0},         # target must be ABOVE entry (long)
    {"target": 90.0},
    {"ticker": "A" * 40},      # ticker over String(16)
])
def test_log_trade_validation_matrix(tmp_path: Path, over: dict[str, object]) -> None:
    # Each Streamlit-form rule, ported: the endpoint rejects with 422 (repo persists
    # blindly, so the Pydantic model is the only gate) and nothing reaches the DB.
    client, engine = _client_and_engine(tmp_path)
    r = client.post("/api/trades", json=_trade_body(**over), headers=_HDR)
    assert r.status_code == 422
    with Session(engine) as s:
        assert s.query(Trade).count() == 0


def test_log_trade_persists_with_engine_defaults(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    r = client.post("/api/trades", json=_trade_body(notes="from cockpit"), headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["entry_date"] == date.today().isoformat()  # server-stamped, never client
    assert body["override"] is None  # unprefilled: no signal to verify against
    with Session(engine) as s:
        t = s.get(Trade, body["trade_id"])
        assert t is not None
        assert t.ticker == "AMD"  # stripped + uppercased
        assert (t.timeframe, t.horizon) == ("1d", "medium")  # engine defaults
        assert t.status == "open" and t.entry_date == date.today()
        assert t.notes == "from cockpit"
        assert t.signal_id is None and t.override is None


def test_log_trade_stamps_override_on_deviation(tmp_path: Path) -> None:
    # Entry above the ceiling in zone-R (risk = ceiling 101 - stop 95 = 6), stop and
    # target moved in % -- the documented format, rendered verbatim by the UI.
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        sig = _signal_row()
        s.add(sig)
        s.commit()
        sig_id = sig.id
    r = client.post("/api/trades", json=_trade_body(
        entry_price=104.0, stop=96.0, target=112.0, signal_id=sig_id), headers=_HDR)
    assert r.status_code == 200
    assert r.json()["override"] == (
        "entry +0.50R above ceiling; stop moved +1.1%; target moved +1.8%"
    )
    with Session(engine) as s:
        t = s.get(Trade, r.json()["trade_id"])
        assert t is not None and t.signal_id == sig_id
        assert t.override == r.json()["override"]  # stored, not just echoed


def test_log_trade_override_below_floor(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        sig = _signal_row()
        s.add(sig)
        s.commit()
        sig_id = sig.id
    # entry below the floor forces the stop down too (long geometry: stop < entry),
    # so this fill deviates on all three legs.
    r = client.post("/api/trades", json=_trade_body(
        entry_price=93.0, stop=92.0, target=105.0, signal_id=sig_id), headers=_HDR)
    assert r.status_code == 200
    # floor 96 - entry 93 = 3 -> 0.50R below; stop (92-95)/95; target (105-110)/110
    assert r.json()["override"] == (
        "entry -0.50R below floor; stop moved -3.2%; target moved -4.5%"
    )


def test_log_trade_override_none_when_faithful(tmp_path: Path) -> None:
    # Entry inside [floor, ceiling], stop/target within float tolerance -> None:
    # a faithful prefilled fill must NOT read as an override.
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        sig = _signal_row()
        s.add(sig)
        s.commit()
        sig_id = sig.id
    r = client.post("/api/trades", json=_trade_body(
        entry_price=100.0, stop=95.001, target=109.999, signal_id=sig_id), headers=_HDR)
    assert r.status_code == 200
    assert r.json()["override"] is None
    with Session(engine) as s:
        t = s.get(Trade, r.json()["trade_id"])
        assert t is not None and t.signal_id == sig_id and t.override is None


def test_log_trade_unknown_signal_id_is_422(tmp_path: Path) -> None:
    # trades.signal_id is a REAL foreign key: sqlite (no FK pragma) would store a
    # dangling id silently while Azure SQL would reject the INSERT as a 503-shaped
    # IntegrityError -- so the endpoint rejects it identically on BOTH backends,
    # before the insert, as input validation.
    client, engine = _client_and_engine(tmp_path)
    r = client.post("/api/trades", json=_trade_body(
        entry_price=104.0, signal_id=999), headers=_HDR)
    assert r.status_code == 422
    assert r.json()["detail"] == "unknown signal_id"
    with Session(engine) as s:
        assert s.query(Trade).count() == 0  # nothing persisted


def _seed_open_trade(engine: Engine, **over: object) -> int:
    row: dict[str, object] = dict(
        ticker="AMD", timeframe="1d", horizon="medium", entry_date=date(2026, 7, 1),
        entry_price=100.0, size=10.0, stop=95.0, target=110.0,
    )
    row.update(over)
    with Session(engine) as s:
        t = Trade(**row)
        s.add(t)
        s.commit()
        return t.id


def test_close_trade_requires_the_cockpit_header(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    tid = _seed_open_trade(engine)
    assert client.post(f"/api/trades/{tid}/close",
                       json={"exit_price": 108.0}).status_code == 403
    with Session(engine) as s:
        t = s.get(Trade, tid)
        assert t is not None and t.status == "open"  # untouched


def test_close_trade_computes_r_and_dollars_and_writes_exit_event(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    tid = _seed_open_trade(engine)
    r = client.post(f"/api/trades/{tid}/close",
                    json={"exit_price": 108.0, "exit_reason": "target"}, headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["trade_id"] == tid
    assert body["realized_r"] == pytest.approx(1.6)     # (108-100)/(100-95)
    assert body["realized_usd"] == pytest.approx(80.0)  # (108-100)*10
    assert body["exit_date"] == date.today().isoformat()  # default: today
    assert body["exit_reason"] == "target"
    with Session(engine) as s:
        t = s.get(Trade, tid)
        assert t is not None and t.status == "closed"
        assert t.exit_price == 108.0 and t.exit_reason == "target"
        events = list(s.query(ExitEvent).all())
    assert len(events) == 1
    ev = events[0]
    assert ev.reason == "manual_close" and ev.is_paper is False
    assert ev.tier == "" and ev.account == "research" and ev.trade_id == tid


def test_close_trade_404_unknown_and_409_already_closed(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    assert client.post("/api/trades/999/close",
                       json={"exit_price": 108.0}, headers=_HDR).status_code == 404
    tid = _seed_open_trade(engine)
    assert client.post(f"/api/trades/{tid}/close",
                       json={"exit_price": 108.0}, headers=_HDR).status_code == 200
    second = client.post(f"/api/trades/{tid}/close",
                         json={"exit_price": 109.0}, headers=_HDR)
    assert second.status_code == 409
    with Session(engine) as s:  # the recorded exit survives the re-close attempt
        t = s.get(Trade, tid)
        assert t is not None and t.exit_price == 108.0
        assert s.query(ExitEvent).count() == 1  # no second event either


@pytest.mark.parametrize("body", [
    {"exit_price": 0.0},
    {"exit_price": -3.0},
    {"exit_price": 108.0, "exit_reason": "x" * 33},  # over String(32)
])
def test_close_trade_validation(tmp_path: Path, body: dict[str, object]) -> None:
    client, engine = _client_and_engine(tmp_path)
    tid = _seed_open_trade(engine)
    r = client.post(f"/api/trades/{tid}/close", json=body, headers=_HDR)
    assert r.status_code == 422
    with Session(engine) as s:
        t = s.get(Trade, tid)
        assert t is not None and t.status == "open"


def test_close_trade_blank_reason_defaults_to_manual(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    tid = _seed_open_trade(engine)
    r = client.post(f"/api/trades/{tid}/close",
                    json={"exit_price": 108.0, "exit_reason": "  "}, headers=_HDR)
    assert r.status_code == 200 and r.json()["exit_reason"] == "manual"


def test_close_trade_null_r_on_degenerate_risk(tmp_path: Path) -> None:
    # A stop raised to/above entry (breakeven management) has no risk denominator:
    # realized_r is an honest null, the close still happens, dollars still compute.
    client, engine = _client_and_engine(tmp_path)
    tid = _seed_open_trade(engine, stop=100.0)  # stop == entry
    r = client.post(f"/api/trades/{tid}/close", json={"exit_price": 108.0}, headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["realized_r"] is None
    assert body["realized_usd"] == pytest.approx(80.0)
    with Session(engine) as s:
        t = s.get(Trade, tid)
        assert t is not None and t.status == "closed"


def test_close_trade_honors_explicit_exit_date(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    tid = _seed_open_trade(engine)
    r = client.post(f"/api/trades/{tid}/close",
                    json={"exit_price": 108.0, "exit_date": "2026-07-09"}, headers=_HDR)
    assert r.status_code == 200 and r.json()["exit_date"] == "2026-07-09"
    with Session(engine) as s:
        t = s.get(Trade, tid)
        assert t is not None and t.exit_date == date(2026, 7, 9)


def test_close_trade_rejects_out_of_range_exit_date(tmp_path: Path) -> None:
    # exit_date is input validation (422), checked post-fetch because the lower
    # bound needs the trade row: never before the entry, never in the future.
    client, engine = _client_and_engine(tmp_path)
    tid = _seed_open_trade(engine)  # entry_date = 2026-07-01
    before = client.post(f"/api/trades/{tid}/close",
                         json={"exit_price": 108.0, "exit_date": "2026-06-30"},
                         headers=_HDR)
    assert before.status_code == 422
    future = (date.today() + timedelta(days=1)).isoformat()
    after = client.post(f"/api/trades/{tid}/close",
                        json={"exit_price": 108.0, "exit_date": future}, headers=_HDR)
    assert after.status_code == 422
    with Session(engine) as s:
        t = s.get(Trade, tid)
        assert t is not None and t.status == "open"  # both rejections left it open
    # the entry day itself is a legal close day (a same-day scratch)
    ok = client.post(f"/api/trades/{tid}/close",
                     json={"exit_price": 108.0, "exit_date": "2026-07-01"}, headers=_HDR)
    assert ok.status_code == 200


def test_close_trade_is_atomic_with_its_exit_event(tmp_path: Path) -> None:
    # ONE transaction carries the trade UPDATE and the ExitEvent INSERT. Two pins:
    # (1) rollback posture -- when the commit fails, NEITHER lands (no closed-but-
    # eventless audit hole: the exit change-token watermark would never move) and
    # the retry finds the trade still open instead of a confusing 409; (2) single-
    # commit discrimination -- with commits failing only from the SECOND call
    # onward, the close still succeeds with both rows on ONE commit, which a
    # close-then-record two-commit implementation cannot do.
    client, engine = _client_and_engine(tmp_path)
    tid = _seed_open_trade(engine)

    def exploding_commit(self: Session) -> None:
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Session, "commit", exploding_commit)
        r = client.post(f"/api/trades/{tid}/close",
                        json={"exit_price": 108.0}, headers=_HDR)
    assert r.status_code == 503  # the app's SQLAlchemyError posture
    with Session(engine) as s:
        t = s.get(Trade, tid)
        assert t is not None and t.status == "open"  # close rolled back...
        assert t.exit_price is None
        assert s.query(ExitEvent).count() == 0       # ...and no orphan event
    # the failed attempt left no half-state: the retry SUCCEEDS
    retry = client.post(f"/api/trades/{tid}/close",
                        json={"exit_price": 108.0}, headers=_HDR)
    assert retry.status_code == 200
    with Session(engine) as s:
        assert s.query(ExitEvent).count() == 1

    # (2) A fresh trade closed while only the FIRST commit can succeed: a two-commit
    # implementation would 503 on its ExitEvent commit and orphan the close; the
    # single-transaction close lands BOTH rows and never asks for a second commit.
    tid2 = _seed_open_trade(engine, ticker="NVDA")
    real_commit = Session.commit
    commits: list[int] = []

    def first_commit_only(self: Session) -> None:
        commits.append(1)
        if len(commits) > 1:
            raise OperationalError("COMMIT", {}, Exception("connection lost"))
        real_commit(self)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Session, "commit", first_commit_only)
        second = client.post(f"/api/trades/{tid2}/close",
                             json={"exit_price": 108.0}, headers=_HDR)
    assert second.status_code == 200
    assert len(commits) == 1  # the close is ONE commit, not close-then-event
    with Session(engine) as s:
        t2 = s.get(Trade, tid2)
        assert t2 is not None and t2.status == "closed"
        assert s.query(ExitEvent).count() == 2  # part (1)'s retry event + this one


# ---- _override_note branch pins (unit level: TradeCreate + an unpersisted Signal) --


def _create_body(**over: object) -> TradeCreate:
    base: dict[str, object] = {"ticker": "AMD", "entry_price": 100.0, "size": 10.0,
                               "stop": 95.0, "target": 110.0}
    base.update(over)
    return TradeCreate(**base)  # type: ignore[arg-type]


def test_override_note_stop_only_is_a_single_part() -> None:
    # entry inside the zone, target faithful: exactly one part, no separators.
    note = _override_note(_create_body(stop=96.0), _signal_row())
    assert note == "stop moved +1.1%"


def test_override_note_degenerate_zone_skips_the_entry_part() -> None:
    # ceiling <= stop leaves no zone-R unit: the entry deviation is unspeakable and
    # skipped (never a divide-by-zero); the % parts still stamp.
    sig = _signal_row(entry_floor=90.0, entry_ceiling=95.0, stop=95.0)
    note = _override_note(_create_body(entry_price=100.0, stop=94.0), sig)
    assert note == "stop moved -1.1%"  # entry is 5.0 above the ceiling, yet no entry part


def test_override_note_suppresses_zero_looking_percent() -> None:
    # a one-cent nudge of a $490 stop clears the ABSOLUTE tolerance but formats as
    # '+0.0%' -- a zero-looking stamp is noise, so the part is suppressed entirely.
    sig = _signal_row(trigger_close=500.0, entry_floor=496.0, entry_ceiling=501.0,
                      stop=490.0, target=550.0)
    body = _create_body(entry_price=500.0, stop=490.01, target=550.0)
    assert _override_note(body, sig) is None


def test_hand_crafted_json_infinity_and_nan_are_422(tmp_path: Path) -> None:
    # httpx's json= refuses non-finite floats, but a hand-crafted body can carry the
    # (non-standard, json.loads-accepted) Infinity/NaN tokens: Infinity passes gt=0
    # and target>entry, NaN compares False against every geometry check -- only the
    # models' allow_inf_nan=False stands between them and the R math.
    client, engine = _client_and_engine(tmp_path)
    hdrs = {**_HDR, "Content-Type": "application/json"}
    raw = ('{"ticker": "AMD", "entry_price": 100.0, "size": 10.0, '
           '"stop": NaN, "target": Infinity}')
    assert client.post("/api/trades", content=raw, headers=hdrs).status_code == 422
    tid = _seed_open_trade(engine)
    r = client.post(f"/api/trades/{tid}/close",
                    content='{"exit_price": Infinity}', headers=hdrs)
    assert r.status_code == 422
    with Session(engine) as s:
        assert s.query(Trade).count() == 1  # only the seeded trade, still open
        t = s.get(Trade, tid)
        assert t is not None and t.status == "open"


def test_mutation_guard_outranks_model_validation(tmp_path: Path) -> None:
    """The _require_cockpit ordering pin: a headerless request with an INVALID-FIELDS
    body is the guard's 403, never a 422 -- decorator dependencies solve ahead of
    model validation. The raw JSON decode DOES precede the guard (FastAPI parses the
    body before solving dependencies), so a syntactically-broken headerless body is
    a 422 -- harmless, nothing side-effectful runs either way."""
    client, engine = _client_and_engine(tmp_path)
    # stop above entry: TradeCreate would 422 this -- the guard must win first.
    r = client.post("/api/trades", json=_trade_body(stop=150.0))
    assert r.status_code == 403
    # the decode-precedes-guard half, pinned honestly rather than overclaimed:
    broken = client.post("/api/trades", content="{not json",
                         headers={"Content-Type": "application/json"})
    assert broken.status_code == 422
    with Session(engine) as s:
        assert s.query(Trade).count() == 0  # neither request touched the DB


# ---- trade READ endpoints: GET /api/positions + GET /api/trade-defaults ----


POSITIONS_KEYS = {"open", "caps", "closed", "equity", "quotes_as_of", "broker_as_of"}
PL_KEYS = {"unrealized_pl", "unrealized_pct", "r_multiple", "dist_to_stop_pct",
           "dist_to_target_pct"}
ROW_KEYS_COMMON = {"kind", "ticker", "timeframe", "entry_price", "size", "stop",
                   "target", "last_close", "pl", "badge", "bracket", "override",
                   "signal_id", "unlinked"}
REAL_ROW_KEYS = ROW_KEYS_COMMON | {"trade_id"}
LIVE_ROW_KEYS = ROW_KEYS_COMMON | {"paper_id"}
CAPS_KEYS = {"account", "run_date", "notional", "loss_r", "concurrent"}
CLOSED_KEYS = {"trade_id", "ticker", "entry_date", "exit_date", "entry_price",
               "exit_price", "size", "realized_usd", "exit_reason"}

_IDEM = itertools.count()


def _positions_client(
    tmp_path: Path, prices: dict[str, float] | None = None,
    broker: FakeBroker | None = None,
) -> tuple[TestClient, Engine]:
    """An app wired through BOTH livedata seams: quotes from a fixed dict (misses
    absent, mirroring latest_closes), broker from a FakeBroker or None -- the suite
    never touches yfinance or a venue."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    fixed = dict(prices or {})
    app = create_app(url, edge_dir=tmp_path,
                     latest_closes_fn=lambda tickers: fixed,
                     broker_factory=lambda: broker)
    return TestClient(app), engine


def _live_paper(**over: object) -> PaperTrade:
    """An OPEN account='live' PaperTrade shaped like reconcile's materialized fill."""
    row: dict[str, object] = dict(
        ticker="LIV", timeframe="1d", horizon="", signal_score=0.0, rank=0,
        account="live", play_type="continuation", fill_status="filled",
        entry_price=50.0, stop=45.0, target=60.0, risk=5.0, status="open",
    )
    row.update(over)
    return PaperTrade(**row)


def _exec_log(**over: object) -> ExecutionLog:
    """One ExecutionLog row; idempotency_key auto-uniqued so seeds never collide."""
    row: dict[str, object] = dict(
        created_date=date(2026, 7, 8), ticker="AMD", timeframe="1d",
        play_type="continuation", run_date=date(2026, 7, 8), account="manual",
        mode="manual", side="buy", limit_price=100.0, shares=10, stop=95.0,
        target=110.0, risk_dollars=50.0, notional=1000.0, status="recorded",
        detail="seeded", idempotency_key=f"seed-{next(_IDEM)}",
    )
    row.update(over)
    return ExecutionLog(**row)


def test_positions_per_row_degradation_and_badges(tmp_path: Path) -> None:
    """One malformed trade (risk <= 0) or one missing quote never 503s the zone:
    every open row is KEPT, the sick one degrades to pl null with the badge STILL
    computed from price/stop/target (a missing quote nulls last_close and reads
    badge 'unknown'), and healthy rows still carry full P/L -- with unrealized_pct
    a FRACTION on the wire, never a percent."""
    client, engine = _positions_client(
        tmp_path, {"GOOD": 104.0, "BADRISK": 50.0, "RED": 94.0, "YEL": 111.0})
    with Session(engine) as s:
        for ticker, stop in (("GOOD", 95.0), ("BADRISK", 100.0), ("NOQUOTE", 95.0),
                             ("RED", 95.0), ("YEL", 95.0)):
            s.add(Trade(ticker=ticker, timeframe="1d", horizon="medium",
                        entry_date=date(2026, 7, 1), entry_price=100.0, size=10.0,
                        stop=stop, target=110.0))
        s.commit()
    r = client.get("/api/positions")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == POSITIONS_KEYS
    rows = {row["ticker"]: row for row in body["open"]}
    assert set(rows) == {"GOOD", "BADRISK", "NOQUOTE", "RED", "YEL"}  # all KEPT
    for row in rows.values():
        assert set(row) == REAL_ROW_KEYS
        assert row["kind"] == "real"
        assert row["unlinked"] is True  # no signal_id on any seeded row

    good = rows["GOOD"]
    assert good["last_close"] == 104.0
    assert set(good["pl"]) == PL_KEYS
    assert good["pl"]["unrealized_pl"] == pytest.approx(40.0)   # (104-100)*10
    assert good["pl"]["unrealized_pct"] == pytest.approx(0.04)  # FRACTION, not 4.0
    assert good["pl"]["r_multiple"] == pytest.approx(0.8)       # (104-100)/(100-95)
    assert good["pl"]["dist_to_stop_pct"] == pytest.approx((104 - 95) / 104)
    assert good["pl"]["dist_to_target_pct"] == pytest.approx((110 - 104) / 104)
    assert good["badge"] == "green"

    bad = rows["BADRISK"]  # stop == entry: position_pl raises ValueError
    assert bad["last_close"] == 50.0  # the quote itself is fine and still shown
    assert bad["pl"] is None
    # The badge reads only price/stop/target, so it SURVIVES broken P/L math: the
    # price (50) breached the recorded stop (100) and that lamp's job is 'get out'.
    assert bad["badge"] == "red"

    noq = rows["NOQUOTE"]  # upstream miss: absent from the price dict
    assert noq["last_close"] is None
    assert noq["pl"] is None and noq["badge"] == "unknown"

    assert rows["RED"]["badge"] == "red"    # 94 <= stop 95
    assert rows["YEL"]["badge"] == "yellow"  # 111 >= target 110
    assert datetime.fromisoformat(body["quotes_as_of"])
    assert body["broker_as_of"] is None  # broker_factory answered None


def test_positions_live_rows_r_only_vs_shares_join(tmp_path: Path) -> None:
    """Live rows have NO size column. The join is spec'd, not improvised: the NEWEST
    ExecutionLog for the ticker with status submitted_live/filled_live supplies
    shares -> dollar P/L; no such row -> size and dollar P/L null with the
    R-multiple still rendered from the persisted per-share ``risk``. An unpriced
    (pending-entry) live row keeps pl null; its badge still reads off the quote."""
    client, engine = _positions_client(
        tmp_path, {"JOINED": 52.0, "LONE": 21.0, "PEND": 52.0})
    with Session(engine) as s:
        s.add(_live_paper(ticker="JOINED"))
        s.add(_live_paper(ticker="LONE", entry_price=20.0, stop=18.0, target=26.0,
                          risk=2.0))
        s.add(_live_paper(ticker="PEND", entry_price=None))
        # JOINED's log history: older filled_live(3) -> newer submitted_live(7) wins;
        # the newest-of-all canceled(99) never reserved shares and must not join,
        # and the even-newer SELL-side live ticket (42) is an exit, not position
        # size -- the join is buy-side only, like repo.latest_recorded_stop.
        s.add(_exec_log(ticker="JOINED", account="live", mode="live", shares=3,
                        status="filled_live"))
        s.add(_exec_log(ticker="JOINED", account="live", mode="live", shares=7,
                        status="submitted_live"))
        s.add(_exec_log(ticker="JOINED", account="live", mode="live", shares=99,
                        status="canceled"))
        s.add(_exec_log(ticker="JOINED", account="live", mode="live", shares=42,
                        side="sell", status="submitted_live"))
        s.commit()
    body = client.get("/api/positions").json()
    rows = {row["ticker"]: row for row in body["open"]}
    for row in rows.values():
        assert set(row) == LIVE_ROW_KEYS
        assert row["kind"] == "live"

    joined = rows["JOINED"]
    assert joined["size"] == 7.0  # the NEWEST counting log, not the older fill
    assert joined["pl"]["unrealized_pl"] == pytest.approx(14.0)  # (52-50)*7
    assert joined["pl"]["r_multiple"] == pytest.approx(0.4)      # (52-50)/risk 5
    assert joined["pl"]["unrealized_pct"] == pytest.approx(0.04)

    lone = rows["LONE"]  # no counting ExecutionLog anywhere
    assert lone["size"] is None
    assert lone["pl"]["unrealized_pl"] is None  # dollar P/L needs shares
    assert lone["pl"]["r_multiple"] == pytest.approx(0.5)  # (21-20)/risk 2
    assert lone["pl"]["unrealized_pct"] == pytest.approx(0.05)

    pend = rows["PEND"]  # entry pending: no P/L to speak of
    assert pend["entry_price"] is None and pend["pl"] is None
    assert pend["badge"] == "green"  # the quote vs stop/target still reads
    assert pend["last_close"] == 52.0


def test_positions_bracket_lamp_per_kind(tmp_path: Path) -> None:
    """Snapshot present: a venue sell stop/stop_limit for the symbol reads 'armed';
    otherwise BOTH kinds read 'db-only' (their stop is a NOT NULL DB column) --
    a REAL/manual row in particular is never 'unprotected': the venue does not
    know it exists, so its stop was only ever a DB number."""
    broker = FakeBroker()
    order = broker.submit_order(BrokerOrderSpec(
        client_order_id="c-1", symbol="LIV", side="buy", qty=7, order_type="limit",
        limit_price=50.0, time_in_force="day", stop_loss=45.0, take_profit=60.0))
    broker.fill(order.broker_order_id, 50.0)  # bracket legs spawn only after fill()
    client, engine = _positions_client(
        tmp_path, {"LIV": 52.0, "NAKED": 21.0, "AMD": 104.0}, broker=broker)
    with Session(engine) as s:
        s.add(_live_paper(ticker="LIV"))
        s.add(_live_paper(ticker="NAKED", entry_price=20.0, stop=18.0, target=26.0,
                          risk=2.0))
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium",
                    entry_date=date(2026, 7, 1), entry_price=100.0, size=10.0,
                    stop=95.0, target=110.0))
        s.commit()
    body = client.get("/api/positions").json()
    rows = {row["ticker"]: row for row in body["open"]}
    assert rows["LIV"]["bracket"] == "armed"     # venue-held sell stop leg
    assert rows["NAKED"]["bracket"] == "db-only"  # live row, no venue stop
    assert rows["AMD"]["bracket"] == "db-only"    # manual row: NEVER 'unprotected'
    assert datetime.fromisoformat(body["broker_as_of"])


def test_positions_bracket_unknown_without_a_broker(tmp_path: Path) -> None:
    """No broker (factory answers None) -> every lamp 'unknown', broker_as_of null:
    absence of evidence is UNKNOWN, never a claim either way."""
    client, engine = _positions_client(tmp_path, {"AMD": 104.0, "LIV": 52.0})
    with Session(engine) as s:
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium",
                    entry_date=date(2026, 7, 1), entry_price=100.0, size=10.0,
                    stop=95.0, target=110.0))
        s.add(_live_paper(ticker="LIV"))
        s.commit()
    body = client.get("/api/positions").json()
    assert all(row["bracket"] == "unknown" for row in body["open"])
    assert body["broker_as_of"] is None


def test_positions_caps_mirror_limit_block_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three gauges read EXACTLY what _limit_block reads: notional sums
    execution_logs_for_day (the repo filters to the counting statuses -- skipped/
    canceled never load), loss is realized_r_on (an R THRESHOLD, today's realized
    R, sign preserved), concurrent is count_open_positions -- all for the account
    the execution MODE maps to, on run_date = latest_run_date."""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "manual")
    monkeypatch.setenv("SWING_MAX_DAILY_NOTIONAL", "5000")
    monkeypatch.setenv("SWING_MAX_DAILY_LOSS", "2")
    monkeypatch.setenv("SWING_MAX_CONCURRENT", "3")
    client, engine = _positions_client(tmp_path)
    latest = date(2026, 7, 8)
    with Session(engine) as s:
        s.add(_signal_row(run_date=date(2026, 7, 1)))
        s.add(_signal_row(run_date=latest, ticker="NVDA"))
        # notional: recorded 1000 + submitted_live 250 count; skipped never reserved;
        # wrong account and wrong run_date stay out of the sum.
        s.add(_exec_log(notional=1000.0, status="recorded"))
        s.add(_exec_log(notional=250.0, status="submitted_live"))
        s.add(_exec_log(notional=400.0, status="skipped"))
        s.add(_exec_log(notional=800.0, account="live"))
        s.add(_exec_log(notional=600.0, run_date=date(2026, 7, 1)))
        # loss: closed manual trades exiting ON the run date, sign preserved.
        s.add(_live_paper(ticker="L1", account="manual", status="closed",
                          exit_date=latest, realized_r=-0.7))
        s.add(_live_paper(ticker="L2", account="manual", status="closed",
                          exit_date=latest, realized_r=0.2))
        s.add(_live_paper(ticker="L3", account="manual", status="closed",
                          exit_date=date(2026, 7, 1), realized_r=-5.0))
        # concurrent: OPEN manual rows only; research rows belong to another book.
        s.add(_live_paper(ticker="O1", account="manual"))
        s.add(_live_paper(ticker="O2", account="manual"))
        s.add(_live_paper(ticker="O3", account="research"))
        s.commit()
    caps = client.get("/api/positions").json()["caps"]
    assert set(caps) == CAPS_KEYS
    assert caps["account"] == "manual"
    assert caps["run_date"] == "2026-07-08"
    assert caps["notional"] == {"used": pytest.approx(1250.0), "limit": 5000.0}
    assert caps["loss_r"] == {"used": pytest.approx(-0.5), "limit": 2.0}
    assert caps["concurrent"] == {"used": 2, "limit": 3}


def test_positions_caps_unbounded_shape_and_no_run_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cap configured -> limit null (NEVER 0: 0 would read as 'nothing allowed');
    mode off maps to the research account; an empty signals table -> run_date null
    with the day-scoped used values an honest 0 (there is no day to sum), while
    concurrent still counts (it is not day-scoped). Empty book: every zone empty,
    quotes_as_of still stamped."""
    for var in ("SWING_EXECUTION_MODE", "SWING_MAX_DAILY_NOTIONAL",
                "SWING_MAX_DAILY_LOSS", "SWING_MAX_CONCURRENT"):
        monkeypatch.delenv(var, raising=False)
    client, engine = _positions_client(tmp_path)
    with Session(engine) as s:
        s.add(_live_paper(ticker="R1", account="research"))  # open research row
        s.commit()
    body = client.get("/api/positions").json()
    caps = body["caps"]
    assert caps["account"] == "research"  # mode 'off' books nothing, reads research
    assert caps["run_date"] is None
    assert caps["notional"] == {"used": 0.0, "limit": None}
    assert caps["loss_r"] == {"used": 0.0, "limit": None}
    assert caps["concurrent"] == {"used": 1, "limit": None}
    assert body["open"] == [] and body["closed"] == [] and body["equity"] == []
    assert datetime.fromisoformat(body["quotes_as_of"])


def test_account_for_mode_covers_every_settings_mode() -> None:
    """_ACCOUNT_FOR_MODE must stay total over the settings mode set: a mode added in
    settings becomes a failure HERE, not a KeyError-500 inside /api/positions."""
    assert set(_ACCOUNT_FOR_MODE) == _EXECUTION_MODES


def test_positions_closed_trades_and_realized_equity(tmp_path: Path) -> None:
    """Closed rows arrive newest-exit first (the repo's order); the equity series is
    the retired _render_closed math -- dated closes ascending, running sum of
    ((exit or entry) - entry) * size, an exit-less close falling back to entry
    (realized 0) and an UNDATED close listed but never plotted."""
    client, engine = _positions_client(tmp_path)

    def closed(ticker: str, exit_price: float | None, exit_date: date | None) -> Trade:
        return Trade(ticker=ticker, timeframe="1d", horizon="medium",
                     entry_date=date(2026, 6, 1), entry_price=100.0, size=10.0,
                     stop=95.0, target=110.0, status="closed", exit_date=exit_date,
                     exit_price=exit_price, exit_reason="target")

    with Session(engine) as s:
        s.add(closed("BBB", 108.0, date(2026, 7, 2)))   # +80
        s.add(closed("AAA", 97.0, date(2026, 7, 1)))    # -30
        s.add(closed("CCC", None, date(2026, 7, 3)))    # exit-less: realized 0
        s.add(closed("DDD", 120.0, None))               # undated: listed, not plotted
        s.commit()
    body = client.get("/api/positions").json()
    by_ticker = {row["ticker"]: row for row in body["closed"]}
    assert set(by_ticker) == {"AAA", "BBB", "CCC", "DDD"}
    for row in body["closed"]:
        assert set(row) == CLOSED_KEYS
    assert by_ticker["BBB"]["realized_usd"] == pytest.approx(80.0)
    assert by_ticker["AAA"]["realized_usd"] == pytest.approx(-30.0)
    assert by_ticker["CCC"]["realized_usd"] == pytest.approx(0.0)
    assert by_ticker["CCC"]["exit_price"] is None
    assert by_ticker["DDD"]["exit_date"] is None
    dated = [row["ticker"] for row in body["closed"] if row["exit_date"] is not None]
    assert dated == ["CCC", "BBB", "AAA"]  # newest exit first
    assert body["equity"] == [["2026-07-01", -30.0], ["2026-07-02", 50.0],
                              ["2026-07-03", 50.0]]


def test_trade_defaults_prefill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The LOG-TRADE form's prefill: the signal's levels verbatim, actionability at
    the cached quote, suggested_entry = the quote CLAMPED into [floor, ceiling], and
    size_order at conviction 'medium' (risk unit 1200 -> medium budget 600 over a
    $6/share zone risk -> 100 shares)."""
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "1200")
    monkeypatch.delenv("SWING_MAX_SHARES", raising=False)
    monkeypatch.delenv("SWING_ACCOUNT_EQUITY", raising=False)
    # zone [96, 101], stop 95, target 110 (the _signal_row geometry); three quotes
    # exercise the three clamp regimes on three separate signals.
    client, engine = _positions_client(
        tmp_path, {"AMD": 104.0, "NVDA": 99.0, "MSFT": 95.5})
    with Session(engine) as s:
        signals = [_signal_row(), _signal_row(ticker="NVDA"),
                   _signal_row(ticker="MSFT")]
        s.add_all(signals)
        s.commit()
        ids = {sig.ticker: sig.id for sig in signals}

    body = client.get(f"/api/trade-defaults?signal_id={ids['AMD']}").json()
    assert set(body) == {"signal", "last_close", "actionability", "suggested_entry",
                         "sizing"}
    assert body["signal"] == {
        "ticker": "AMD", "timeframe": "1d", "horizon": "medium",
        "play_type": "continuation", "entry_floor": 96.0, "entry_ceiling": 101.0,
        "stop": 95.0, "target": 110.0, "conviction_tier": "base"}
    assert body["last_close"] == 104.0
    # 104 sits (104-101)/6 = 0.5R past the ceiling -- extended, and the prefill
    # refuses to chase: suggested entry clamps DOWN to the ceiling.
    assert body["actionability"] == {"status": "extended",
                                     "dist_r": pytest.approx(0.5)}
    assert body["suggested_entry"] == 101.0
    assert body["sizing"] == {"shares": 100, "risk_dollars": pytest.approx(600.0),
                              "unconfigured": False}

    inside = client.get(f"/api/trade-defaults?signal_id={ids['NVDA']}").json()
    assert inside["actionability"]["status"] == "actionable"
    assert inside["suggested_entry"] == 99.0  # in the zone: the quote itself

    below = client.get(f"/api/trade-defaults?signal_id={ids['MSFT']}").json()
    assert below["actionability"]["status"] == "actionable"  # above stop, room to enter
    assert below["suggested_entry"] == 96.0  # clamps UP to the floor


def test_trade_defaults_sizing_unconfigured_no_quote_and_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shares == 0 is the deliberate 'sizing unconfigured' signal (no risk unit set:
    the UI renders R-multiples, never a guessed dollar); a missing quote nulls
    last_close, actionability, AND suggested_entry; an unknown signal_id is a 404
    and a missing/garbage one FastAPI's 422."""
    for var in ("SWING_RISK_PER_TRADE_DOLLARS", "SWING_ACCOUNT_EQUITY"):
        monkeypatch.delenv(var, raising=False)
    client, engine = _positions_client(tmp_path)  # no quotes at all
    with Session(engine) as s:
        sig = _signal_row()
        s.add(sig)
        s.commit()
        sig_id = sig.id
    body = client.get(f"/api/trade-defaults?signal_id={sig_id}").json()
    assert body["sizing"] == {"shares": 0, "risk_dollars": 0.0, "unconfigured": True}
    assert body["last_close"] is None
    assert body["actionability"] is None
    assert body["suggested_entry"] is None
    assert client.get("/api/trade-defaults?signal_id=999").status_code == 404
    assert client.get("/api/trade-defaults").status_code == 422
    assert client.get("/api/trade-defaults?signal_id=abc").status_code == 422


# ---- deep analysis: POST/GET /api/analysis + the chart/PDF byte proxies ----

# 1x1 transparent PNG -- valid bytes (same fixture as tests/test_storage_blob.py).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PDF = b"%PDF-1.4 fake report bytes"

ANALYSIS_ROW_KEYS = {"id", "ticker", "status", "stalled", "requested_at", "started_at",
                     "finished_at", "summary", "error", "has_pdf", "chart_count"}


def _analysis_row(**over: object) -> AnalysisRequest:
    """One queue row; datetimes are NAIVE, exactly as sqlite/mssql hand them back."""
    row: dict[str, object] = dict(
        ticker="AMD", requested_at=datetime(2026, 7, 10, 12, 0), status="queued")
    row.update(over)
    return AnalysisRequest(**row)


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
            ticker="OLD", requested_at=datetime(2026, 7, 9, 12, 0), status="done",
            started_at=datetime(2026, 7, 9, 12, 5),
            finished_at=datetime(2026, 7, 9, 12, 9), summary="looks fine",
            pdf_blob_key="20260709/OLD_report.pdf",
            chart_blob_keys="a.png, b.png,,"))  # split, strip, drop empties -> 2
        s.add(_analysis_row(ticker="NEW", requested_at=datetime(2026, 7, 10, 12, 0)))
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
                                requested_at=datetime(2026, 7, 10, 12, i)))
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


# ---- proposals: GET /api/proposals + the approve/withdraw decisions ----

# The proposal wire form -- a closed set, like STAT_KEYS: the 8 stored
# ProposedVariant fields plus the three derived decision aids.
PROPOSAL_KEYS = {"name", "play_type", "delta", "rationale", "hunch_ref", "status",
                 "drafted_at", "provenance", "gate_verdict", "delta_vs_incumbent",
                 "noop"}

# Both decision responses carry this honesty line verbatim: decide_proposal edits
# the WORKING TREE only -- git capturing the flip is the human's move.
_NOTE = "uncommitted working-tree edit — commit with your decision"


def _proposal(name: str, play_type: str,
              delta: dict[str, float | int | str | bool],
              *, status: str = "queued") -> ProposedVariant:
    return ProposedVariant(
        name=name, play_type=play_type, delta=delta, rationale="hunch text",
        hunch_ref="reversal:2026-07-01:3", status=status,
        drafted_at="2026-07-10", provenance="analyst:test",
    )


def _write_proposals(edge_dir: Path, play_type: str,
                     items: list[ProposedVariant]) -> None:
    (edge_dir / store_filename(play_type)).write_text(
        proposed_to_json(items), encoding="utf-8")


def test_proposals_list_both_play_types_in_file_order(tmp_path: Path) -> None:
    """GET /api/proposals returns continuation then reversal, file order within;
    every row is the closed 11-key wire set with the 8 stored fields verbatim."""
    _write_proposals(tmp_path, "continuation",
                     [_proposal("c1", "continuation", {"max_extension_atr": 1.5})])
    _write_proposals(tmp_path, "reversal", [
        _proposal("r2", "reversal", {"reversal_confirm_window": 5}),
        _proposal("r1", "reversal", {"min_target_r": 2.0}),
    ])
    r = _client(tmp_path).get("/api/proposals")
    assert r.status_code == 200
    assert r.json()["store_errors"] == []  # both stores healthy
    rows = r.json()["proposals"]
    assert [(row["play_type"], row["name"]) for row in rows] == [
        ("continuation", "c1"), ("reversal", "r2"), ("reversal", "r1")]
    assert all(set(row) == PROPOSAL_KEYS for row in rows)
    c1 = rows[0]
    assert c1["delta"] == {"max_extension_atr": 1.5}
    assert c1["rationale"] == "hunch text"
    assert c1["hunch_ref"] == "reversal:2026-07-01:3"
    assert c1["status"] == "queued"
    assert c1["drafted_at"] == "2026-07-10"
    assert c1["provenance"] == "analyst:test"


def test_proposals_gate_verdict_and_noop(tmp_path: Path) -> None:
    """``gate_verdict`` runs the REAL gatekeeper per row: 'ok' for a legal delta,
    the ValueError text for an unknown knob or a frozen indicator period. ``noop``
    mirrors build_config_grid's incumbent-equality skip and never marks an invalid
    row (a row that fails the gate is invalid, not a no-op)."""
    _write_proposals(tmp_path, "reversal", [
        _proposal("legal", "reversal", {"max_extension_atr": 1.5}),
        _proposal("bogus", "reversal", {"no_such_knob": 1}),
        _proposal("frozen", "reversal", {"ema_fast": 10}),
        _proposal("same", "reversal",
                  {"max_extension_atr": StrategyConfig().max_extension_atr}),
    ])
    rows = {row["name"]: row for row in
            _client(tmp_path).get("/api/proposals").json()["proposals"]}
    assert rows["legal"]["gate_verdict"] == "ok"
    assert rows["legal"]["noop"] is False
    assert "no_such_knob" in rows["bogus"]["gate_verdict"]
    assert rows["bogus"]["noop"] is False
    assert "indicator field" in rows["frozen"]["gate_verdict"]
    assert rows["frozen"]["noop"] is False
    assert rows["same"]["gate_verdict"] == "ok"
    assert rows["same"]["noop"] is True


def test_proposals_delta_vs_incumbent(tmp_path: Path) -> None:
    """Each delta knob becomes {knob, current, proposed}: ``current`` is the
    incumbent StrategyConfig default; an unknown knob's current is null (the
    gate_verdict already names the error)."""
    _write_proposals(tmp_path, "continuation", [
        _proposal("mix", "continuation",
                  {"max_extension_atr": 1.5, "no_such_knob": 9}),
    ])
    row = _client(tmp_path).get("/api/proposals").json()["proposals"][0]
    assert row["delta_vs_incumbent"] == [
        {"knob": "max_extension_atr",
         "current": StrategyConfig().max_extension_atr, "proposed": 1.5},
        {"knob": "no_such_knob", "current": None, "proposed": 9},
    ]


def test_proposals_missing_files_are_empty_not_errors(tmp_path: Path) -> None:
    """No proposed.json anywhere -> empty rows AND empty store_errors (missing is
    'nothing queued', never corruption); one play type missing -> only the
    other's rows (a play type with nothing queued is the common case)."""
    client = _client(tmp_path)
    assert client.get("/api/proposals").json() == {
        "proposals": [], "store_errors": []}
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    body = client.get("/api/proposals").json()
    assert [row["name"] for row in body["proposals"]] == ["r1"]
    assert body["store_errors"] == []


# Both corruption flavors of a proposal store, keyed by which exception the load
# raises: malformed JSON (json.JSONDecodeError -- a ValueError SUBCLASS, the 409
# shadowing trap) and syntactically-valid JSON whose row ProposedVariant(**d)
# can't rebuild (TypeError).
_CORRUPT_STORES = [
    pytest.param("{not json", id="malformed-json"),
    pytest.param('[{"name": "x", "bogus_field": 1}]', id="mangled-row"),
]


@pytest.mark.parametrize("corrupt", _CORRUPT_STORES)
def test_proposals_get_corrupt_store_degrades_per_play_type(
    tmp_path: Path, corrupt: str,
) -> None:
    """One corrupt store never blanks the screen: the healthy play type still
    renders and ``store_errors`` names the broken one -- play-type name ONLY,
    never the parser message or a path."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    (tmp_path / store_filename("continuation")).write_text(
        corrupt, encoding="utf-8")
    r = _client(tmp_path).get("/api/proposals")
    assert r.status_code == 200
    body = r.json()
    assert [row["name"] for row in body["proposals"]] == ["r1"]
    assert body["store_errors"] == ["continuation"]
    assert tmp_path.name not in r.text  # leak posture holds on the degrade path


@pytest.mark.parametrize("corrupt", _CORRUPT_STORES)
def test_proposal_decision_corrupt_store_is_503_never_409(
    tmp_path: Path, corrupt: str,
) -> None:
    """Both corruption flavors on POST are the FIXED 503 detail -- never a 409
    (json.JSONDecodeError IS a ValueError: mapped after the state-conflict arm, a
    typo'd store would read as 'already decided') and never a 500. The store's
    bytes are untouched: fixing the file by hand is the whole recovery path."""
    (tmp_path / store_filename("reversal")).write_text(corrupt, encoding="utf-8")
    client = _client(tmp_path)
    for action in ("approve", "withdraw"):
        r = client.post(f"/api/proposals/reversal/r1/{action}",
                        json={"reason": "x"}, headers=_HDR)
        assert r.status_code == 503
        assert r.json()["detail"] == (
            "proposal store unreadable -- fix edge/reversal.proposed.json by hand")
    stored = (tmp_path / store_filename("reversal")).read_text(encoding="utf-8")
    assert stored == corrupt


def test_promotion_checklist_names_real_registry_fields() -> None:
    """Drift guard: the checklist's registry-row line names Experiment fields
    (stopping rule, mde_r, target_ci_halfwidth_r, registered sha) as prose -- a
    registry field rename must break HERE, not silently rot the checklist text."""
    assert {"stopping_rule", "mde_r", "target_ci_halfwidth_r", "registered_sha"} <= {
        f.name for f in fields(Experiment)}


def test_approve_proposal_rewrites_the_store_with_the_checklist(
    tmp_path: Path,
) -> None:
    """Approve MARKS the row (status flip + rationale audit append, the store
    rewritten in place) and returns the verbatim three-artifact promotion
    checklist -- it never touches variants.py or experiments.json itself.
    ``file`` is the repo-relative label; the resolved edge dir never leaks."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    client = _client(tmp_path)
    r = client.post("/api/proposals/reversal/r1/approve",
                    json={"reason": "worth a slot"}, headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "r1"
    assert body["play_type"] == "reversal"
    assert body["status"] == "approved"
    assert body["file"] == "edge/reversal.proposed.json"
    assert body["note"] == _NOTE
    assert body["checklist"] == [
        "1. pipeline/variants.py — add the roster line (replace(base, **delta))",
        "2. edge/experiments.json — add the registry row (stopping rule, mde_r, "
        "target_ci_halfwidth_r, registered sha)",
        "3. edge/reversal.proposed.json — this flip (done)",
    ]
    # Leak posture: the resolved edge_dir (tmp_path) must not appear on the wire.
    assert tmp_path.name not in r.text
    (reloaded,) = load_proposed_for("reversal", tmp_path)
    assert reloaded.status == "approved"
    assert reloaded.rationale.endswith(
        f" APPROVED {date.today().isoformat()}: worth a slot")


def test_withdraw_proposal_and_withdraw_after_approve(tmp_path: Path) -> None:
    """Withdraw works from queued AND from approved (an approval can be walked
    back); the response carries file + note but NO checklist, and the walked-back
    row keeps both audit appends."""
    _write_proposals(tmp_path, "continuation", [
        _proposal("direct", "continuation", {"max_extension_atr": 1.5}),
        _proposal("walked_back", "continuation", {"max_extension_atr": 1.0}),
    ])
    client = _client(tmp_path)
    r = client.post("/api/proposals/continuation/direct/withdraw",
                    json={"reason": "superseded"}, headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "withdrawn"
    assert body["file"] == "edge/continuation.proposed.json"
    assert body["note"] == _NOTE
    assert "checklist" not in body
    ok = client.post("/api/proposals/continuation/walked_back/approve",
                     json={"reason": "test it"}, headers=_HDR)
    assert ok.status_code == 200
    r2 = client.post("/api/proposals/continuation/walked_back/withdraw",
                     json={"reason": "changed my mind"}, headers=_HDR)
    assert r2.status_code == 200
    assert r2.json()["status"] == "withdrawn"
    rows = {pv.name: pv for pv in load_proposed_for("continuation", tmp_path)}
    assert rows["direct"].status == "withdrawn"
    assert rows["walked_back"].status == "withdrawn"
    assert " APPROVED " in rows["walked_back"].rationale  # the audit trail survives
    assert " WITHDRAWN " in rows["walked_back"].rationale


def test_proposal_decision_404_on_unknown_name(tmp_path: Path) -> None:
    """An unknown name is decide_proposal's KeyError, surfaced as a 404 carrying
    the exception's own message -- unquoted (args[0], never str(KeyError)). A
    missing store file is the same 404 (load_proposed_for reads it as [])."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    client = _client(tmp_path)
    r = client.post("/api/proposals/reversal/nope/approve",
                    json={"reason": "x"}, headers=_HDR)
    assert r.status_code == 404
    assert r.json()["detail"] == "no proposal named 'nope' for reversal"
    r2 = client.post("/api/proposals/continuation/ghost/withdraw",
                     json={"reason": "x"}, headers=_HDR)
    assert r2.status_code == 404


def test_proposal_decision_409_on_wrong_state(tmp_path: Path) -> None:
    """A refused transition is decide_proposal's ValueError, surfaced 409: approve
    is queued-only (re-approve refuses), and a withdrawn row refuses BOTH verbs
    (a withdrawal is final until a human hand-edits it). Refusals never write."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    client = _client(tmp_path)
    assert client.post("/api/proposals/reversal/r1/approve",
                       json={"reason": "first"}, headers=_HDR).status_code == 200
    again = client.post("/api/proposals/reversal/r1/approve",
                        json={"reason": "second"}, headers=_HDR)
    assert again.status_code == 409
    assert "its status is 'approved'" in again.json()["detail"]
    assert client.post("/api/proposals/reversal/r1/withdraw",
                       json={"reason": "walk back"}, headers=_HDR).status_code == 200
    for action in ("approve", "withdraw"):
        r = client.post(f"/api/proposals/reversal/r1/{action}",
                        json={"reason": "again"}, headers=_HDR)
        assert r.status_code == 409
        assert "its status is 'withdrawn'" in r.json()["detail"]
    (row,) = load_proposed_for("reversal", tmp_path)
    assert row.rationale.count("WITHDRAWN") == 1  # refused transitions never write


def test_proposal_decisions_require_the_cockpit_header(tmp_path: Path) -> None:
    """Headerless approve/withdraw die at the guard (403) before any store read;
    the row stays queued."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    client = _client(tmp_path)
    for action in ("approve", "withdraw"):
        r = client.post(f"/api/proposals/reversal/r1/{action}",
                        json={"reason": "x"})
        assert r.status_code == 403
    (row,) = load_proposed_for("reversal", tmp_path)
    assert row.status == "queued"


@pytest.mark.parametrize("url,body", [
    ("/api/proposals/reversal/r1/approve", {"reason": ""}),
    ("/api/proposals/reversal/r1/approve", {"reason": "   "}),
    ("/api/proposals/reversal/r1/withdraw", {"reason": "x" * 201}),
    ("/api/proposals/reversal/r1/approve", {}),
    ("/api/proposals/daytrade/r1/approve", {"reason": "x"}),
])
def test_proposal_decision_validation(tmp_path: Path, url: str,
                                      body: dict[str, object]) -> None:
    """Blank/missing/overlong reason and an unknown play_type are 422s (the model's
    strip + bounds; the Literal path param); none of them reach the store."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    client = _client(tmp_path)
    assert client.post(url, json=body, headers=_HDR).status_code == 422
    (row,) = load_proposed_for("reversal", tmp_path)
    assert row.status == "queued"

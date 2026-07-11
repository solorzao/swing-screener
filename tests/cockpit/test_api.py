"""Cockpit API contract: health never lies or leaks, heartbeats and stats are typed,
and a dead database is a friendly 503 -- never a traceback, never the URL."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import COST_STAMPED_FROM
from swing_screener.cockpit.api import (
    _LOGIN_TTL_S,
    _STATE_RANK,
    _down_summary,
    _LoginFlight,
    connection_label,
    create_app,
)
from swing_screener.cockpit.settlement import STATES
from swing_screener.db.models import EmailLog, PaperTrade
from swing_screener.db.repo import save_reversal_funnel
from swing_screener.db.session import get_engine
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

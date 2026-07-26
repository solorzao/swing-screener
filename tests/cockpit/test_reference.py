"""Reference router (split from test_api.py): /api/ticker, /api/exits,
/api/universe, /api/emails, and the stored-error leak posture."""

from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from swing_screener.cockpit.heartbeats import _eod_utc
from swing_screener.cockpit.routers.reference import BOOKKEEPING_EMAIL_KINDS, _day_ts
from swing_screener.db.models import (
    EmailLog,
    ExitEvent,
    Universe,
)
from swing_screener.notify import alerts
from tests.cockpit.conftest import (
    _analysis_row,
    _client_and_engine,
    _exec_log,
    _grade_call,
)

TICKER_BASE_KEYS = {"source", "ts", "ticker", "headline", "detail"}
EXIT_ROW_KEYS = {"id", "date", "trade_id", "is_paper", "account", "tier", "reason",
                 "message"}
UNIVERSE_ROW_KEYS = {"ticker", "name", "exchange", "market_cap", "avg_dollar_volume",
                     "sector"}
EMAIL_ROW_KEYS = {"id", "sent_at", "kind", "subject", "run_date"}

# ---- /api/ticker, /api/exits, /api/universe, /api/emails


def test_ticker_merges_sources_desc(tmp_path: Path) -> None:
    """All five sources merge newest-first: DATE-only stamps anchor at end-of-day
    UTC, an AnalysisRequest rides its most recent lifecycle stamp (finished beats
    requested), and exit rows carry the four facet fields on top of the base
    shape. ``limit`` trims the MERGED list."""
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(ExitEvent(created_date=date(2026, 7, 8), is_paper=True,
                        account="research", tier="t1", reason="stop_violation",
                        message="AAA stopped"))
        s.add(_exec_log(created_date=date(2026, 7, 9)))
        s.add(EmailLog(sent_at=datetime(2026, 7, 10, 12, 0), kind="daily",  # noqa: DTZ001 -- naive test fixture
                       subject="Digest", run_date=date(2026, 7, 9)))
        s.add(_grade_call("NVO", run_date=date(2026, 7, 7)))
        # requested LONG ago but finished recently: the finish is the event
        s.add(_analysis_row(requested_at=datetime(2026, 7, 1, 9, 0), status="done",  # noqa: DTZ001 -- naive test fixture
                            finished_at=datetime(2026, 7, 10, 15, 0),  # noqa: DTZ001 -- naive test fixture
                            summary="done summary"))
        s.commit()
    r = client.get("/api/ticker")
    assert r.status_code == 200
    events = r.json()["events"]
    assert [e["source"] for e in events] == [
        "analysis", "email", "execution", "exit", "analyst"]
    for e in events:
        assert datetime.fromisoformat(e["ts"]).tzinfo is not None
    exit_row = events[3]
    assert set(exit_row) == TICKER_BASE_KEYS | {"account", "is_paper", "reason",
                                                "tier"}
    assert exit_row["ticker"] is None  # ExitEvent has no ticker column
    assert exit_row["headline"] == "AAA stopped"
    assert (exit_row["account"], exit_row["is_paper"], exit_row["reason"],
            exit_row["tier"]) == ("research", True, "stop_violation", "t1")
    assert set(events[0]) == TICKER_BASE_KEYS  # non-exit rows: the base shape only
    assert events[0]["ticker"] == "AMD" and "done" in events[0]["headline"]
    assert events[2]["headline"] == "buy 10 AMD @ 100 (recorded)"

    trimmed = client.get("/api/ticker", params={"limit": 2}).json()["events"]
    assert [e["source"] for e in trimmed] == ["analysis", "email"]
    assert client.get("/api/ticker", params={"limit": 0}).status_code == 422


def test_day_ts_locksteps_with_eod_utc() -> None:
    """The ticker's date anchor is the heartbeats' ``_eod_utc`` rule, restated for
    a non-optional date -- this pin keeps the two from drifting."""
    assert _day_ts(date(2026, 7, 8)) == _eod_utc(date(2026, 7, 8))


def test_exits_three_facets(tmp_path: Path) -> None:
    """The Exit Log's three facets, server-side, on DIFFERENT axes: ``book`` is
    is_paper (the research grid AND the curated intent book are both paper),
    ``account`` splits those two, ``reason`` is an exact match. Filters compose;
    absent means all; newest first with id breaking same-day ties."""
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(ExitEvent(created_date=date(2026, 7, 5), is_paper=True,
                        account="research", tier="t1", reason="stop_violation"))
        s.add(ExitEvent(created_date=date(2026, 7, 6), is_paper=True,
                        account="paper", tier="t1", reason="target"))
        s.add(ExitEvent(created_date=date(2026, 7, 7), is_paper=False,
                        account="research", tier="", reason="stop_violation"))
        s.add(ExitEvent(created_date=date(2026, 7, 8), is_paper=False,
                        account="research", tier="", reason="manual_close"))
        s.commit()

    def dates(**params: object) -> list[str]:
        r = client.get("/api/exits", params=params)  # type: ignore[arg-type]
        assert r.status_code == 200
        return [row["date"] for row in r.json()["exits"]]

    assert dates() == ["2026-07-08", "2026-07-07", "2026-07-06", "2026-07-05"]
    assert dates(reason="stop_violation") == ["2026-07-07", "2026-07-05"]
    assert dates(book="real") == ["2026-07-08", "2026-07-07"]
    assert dates(book="paper") == ["2026-07-06", "2026-07-05"]
    assert dates(account="paper") == ["2026-07-06"]  # a PAPER-book facet, not book
    assert dates(reason="stop_violation", book="paper") == ["2026-07-05"]
    assert dates(limit=1) == ["2026-07-08"]

    row = client.get("/api/exits").json()["exits"][0]
    assert set(row) == EXIT_ROW_KEYS
    assert client.get("/api/exits", params={"book": "shadow"}).status_code == 422


def test_universe_search_parity_and_sector(tmp_path: Path) -> None:
    """Ticker search IS the repo's ``list_universe`` LIKE (uppercased, wildcard-
    escaped) -- a '%' search matches a literal percent (nothing here), never
    everything. ``sector`` -- stored but never displayed by the retired page --
    now rides the wire."""
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(Universe(ticker="AMD", name="Advanced Micro Devices",
                       exchange="NASDAQ", market_cap=2.5e11,
                       avg_dollar_volume=5e9, sector="Information Technology"))
        s.add(Universe(ticker="AMAT", name="Applied Materials", exchange="NASDAQ"))
        s.add(Universe(ticker="NVDA", name="NVIDIA", exchange="NASDAQ"))
        s.commit()
    rows = client.get("/api/universe").json()["rows"]
    assert [u["ticker"] for u in rows] == ["AMAT", "AMD", "NVDA"]  # ticker-ordered
    amd = {u["ticker"]: u for u in rows}["AMD"]
    assert set(amd) == UNIVERSE_ROW_KEYS
    assert amd["sector"] == "Information Technology"
    assert amd["market_cap"] == 2.5e11
    assert {u["ticker"]: u for u in rows}["AMAT"]["sector"] is None

    hits = client.get("/api/universe", params={"search": "am"}).json()["rows"]
    assert [u["ticker"] for u in hits] == ["AMAT", "AMD"]  # case-folded LIKE
    assert client.get("/api/universe",
                      params={"search": "%"}).json()["rows"] == []  # escaped
    assert client.get("/api/universe",
                      params={"search": "zz"}).json()["rows"] == []


def test_emails_limit_default_100(tmp_path: Path) -> None:
    """``list_email_log`` is unbounded, so the endpoint runs its own LIMITed
    SELECT: default 100, newest sent first, ``sent_at`` served as unambiguous
    UTC."""
    client, engine = _client_and_engine(tmp_path)
    base = datetime(2026, 7, 1, 8, 0)  # noqa: DTZ001 -- naive test fixture
    with Session(engine) as s:
        for i in range(105):
            s.add(EmailLog(sent_at=base + timedelta(minutes=i), kind="daily",
                           subject=f"digest {i}", alert_key=f"k{i}"))
        s.commit()
    emails = client.get("/api/emails").json()["emails"]
    assert len(emails) == 100  # the default bound
    assert set(emails[0]) == EMAIL_ROW_KEYS
    assert emails[0]["subject"] == "digest 104"  # newest first
    assert emails[0]["sent_at"].endswith("+00:00")
    assert emails[-1]["subject"] == "digest 5"
    assert len(client.get("/api/emails", params={"limit": 5}).json()["emails"]) == 5
    assert client.get("/api/emails", params={"limit": 0}).status_code == 422


def test_email_surfaces_hide_per_row_rejection_coverage(tmp_path: Path) -> None:
    """``execution-cover`` rows are BOOKKEEPING, not sent emails: the
    live-rejection alert writes one per alerted ExecutionLog id so the hourly
    retry can join on coverage (``notify.alerts``), plus ONE ``execution``
    display row for the email itself. Both surfaces that render EmailLog -- the
    digest log and the Zone E ticker -- must show the one email, not N rows."""
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(EmailLog(sent_at=datetime(2026, 7, 10, 12, 0), kind="execution",
                       subject="Swing Screener — 3 Live Orders Rejected",
                       run_date=date(2026, 7, 10), alert_key="sethash"))
        for i in (11, 12, 13):
            s.add(EmailLog(sent_at=datetime(2026, 7, 10, 12, 0), kind="execution-cover",
                           subject="Swing Screener — 3 Live Orders Rejected",
                           run_date=date(2026, 7, 10), alert_key=f"xlog-{i}"))
        s.commit()

    emails = client.get("/api/emails").json()["emails"]
    assert [m["kind"] for m in emails] == ["execution"]  # ONE row per email sent
    ticker = client.get("/api/ticker").json()["events"]
    assert [e["detail"] for e in ticker if e["source"] == "email"] == ["execution"]
    # the router restates the kind as a literal (no cockpit -> notify import);
    # this is the anti-drift pin against the module that WRITES those rows.
    assert alerts.REJECTION_COVER_KIND in BOOKKEEPING_EMAIL_KINDS


# ---- review fixes: stored-error leak posture + the lifecycle-ordered window


def test_ticker_execution_detail_shims_historical_broker_errors(
    tmp_path: Path,
) -> None:
    """Pre-fix ExecutionLog rows carry ``broker error: <raw message>`` with the
    venue host embedded -- the wire serves the bare label. Post-fix rows
    (class-name-only) and ordinary details pass through verbatim: the shim is
    read-time only, for rows persisted before the write-time fix."""
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(_exec_log(detail="broker error: secret-venue.example 401 denied"))
        s.add(_exec_log(detail="broker error (RuntimeError)"))
        s.add(_exec_log(detail="order submitted"))
        s.commit()
    r = client.get("/api/ticker")
    details = sorted(e["detail"] for e in r.json()["events"]
                     if e["source"] == "execution")
    assert details == ["broker error", "broker error (RuntimeError)",
                       "order submitted"]
    assert "secret-venue" not in r.text


def test_ticker_analysis_window_orders_by_lifecycle_not_id(tmp_path: Path) -> None:
    """The AnalysisRequest sub-select orders by the coalesced lifecycle stamp
    (finished > started > requested): an OLD request that finished today beats a
    NEWER id still queued -- under the old id-desc order it would be starved out
    of a limit-1 candidate window entirely."""
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(_analysis_row(ticker="OLD", requested_at=datetime(2026, 7, 1, 9, 0),  # noqa: DTZ001 -- naive test fixture
                            status="done",
                            finished_at=datetime(2026, 7, 10, 15, 0)))  # noqa: DTZ001 -- naive test fixture
        s.add(_analysis_row(ticker="NEW", requested_at=datetime(2026, 7, 9, 9, 0)))  # noqa: DTZ001 -- naive test fixture
        s.commit()
    events = client.get("/api/ticker", params={"limit": 1}).json()["events"]
    assert [e["ticker"] for e in events] == ["OLD"]  # id-desc would starve OLD out



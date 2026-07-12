"""Task 10 contract: the cockpit Journal API router.

Reads are 503-friendly and every STATISTIC crosses the wire as a full 10-key Stat
(breakdowns, mistake rows); calendar cells, curve points, excursion/discipline
summaries, notes and records are plain hand-rolled dicts. Writes (POST notes, POST
tag) require the ``X-Cockpit`` header -- headerless is a 403 before anything runs.
"""

from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import create_app
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.journal.repo import add_tag, tag_trade

STAT_KEYS = {"value", "n", "n_clusters", "ci_low", "ci_high", "cost_level",
             "corpus_id", "facet", "unit", "thin_clusters"}
_HDR = {"X-Cockpit": "1"}


def _db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"


def _client(tmp_path: Path) -> TestClient:
    url = _db_url(tmp_path)
    get_engine(url)
    return TestClient(create_app(url, edge_dir=tmp_path))


def _pt(ticker: str, r: float | None, *, account: str = "research",
        status: str = "closed", exit_date: date | None = None, hold_bars: int | None = None,
        entry_price: float | None = None, low_water: float | None = None,
        high_water: float | None = None, exit_reason: str | None = None) -> PaperTrade:
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account=account, fill_status="filled", stop=95.0, target=110.0, risk=5.0,
        status=status, realized_r=r, exit_date=exit_date, hold_bars=hold_bars,
        entry_price=entry_price, low_water=low_water, high_water=high_water,
        exit_reason=exit_reason, opened_date=exit_date,
    )


def _seed(url: str, trades: list[PaperTrade]) -> None:
    with Session(get_engine(url)) as s:
        s.add_all(trades)
        s.commit()


def test_calendar_groups_by_day_and_month(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    _seed(url, [
        _pt("AMD", 2.0, exit_date=date(2026, 1, 5)),
        _pt("NVDA", -1.0, exit_date=date(2026, 1, 5)),
        _pt("MSFT", 1.5, exit_date=date(2026, 2, 10)),
    ])
    r = _client(tmp_path).get("/api/journal/calendar?book=research")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"days", "months", "cost_level"}
    assert body["days"]["2026-01-05"] == {"r": 1.0, "n": 2}
    assert set(body["months"]) == {"2026-01", "2026-02"}
    # month filter narrows the day grid but not the month roll-up
    r2 = _client(tmp_path).get("/api/journal/calendar?book=research&month=2026-02")
    days = r2.json()["days"]
    assert set(days) == {"2026-02-10"}


def test_calendar_bad_month_is_422(tmp_path: Path) -> None:
    assert _client(tmp_path).get(
        "/api/journal/calendar?book=research&month=nonsense").status_code == 422


def test_curve_carries_drawdown(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    _seed(url, [
        _pt("AMD", 1.0, exit_date=date(2026, 1, 1)),
        _pt("AMD", 2.0, exit_date=date(2026, 1, 2)),
        _pt("AMD", -3.0, exit_date=date(2026, 1, 3)),
    ])
    body = _client(tmp_path).get("/api/journal/curve?book=research").json()
    assert set(body) == {"curve", "drawdown", "max_drawdown"}
    assert body["curve"] == [["2026-01-01", 1.0], ["2026-01-02", 3.0], ["2026-01-03", 0.0]]
    assert body["max_drawdown"] == 3.0


def test_excursions_summary(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    # entry 100, risk 5, low_water 97.5 -> mae 0.5R; high_water 110 -> mfe 2.0R
    _seed(url, [_pt("AMD", 1.0, exit_date=date(2026, 1, 1),
                    entry_price=100.0, low_water=97.5, high_water=110.0)])
    body = _client(tmp_path).get("/api/journal/excursions?book=research").json()
    assert set(body) == {"n", "avg_mae_r", "avg_mfe_r", "median_mae_r", "median_mfe_r"}
    assert body["n"] == 1
    assert body["avg_mae_r"] == 0.5
    assert body["avg_mfe_r"] == 2.0


def test_breakdowns_each_bucket_is_a_stat(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    _seed(url, [
        _pt("AMD", 2.0, exit_date=date(2026, 1, 5), hold_bars=1),   # Mon, 1-1
        _pt("NVDA", -1.0, exit_date=date(2026, 1, 7), hold_bars=4),  # Wed, 4-5
    ])
    client = _client(tmp_path)
    for by, some_label in [("dow", "Mon"), ("hold", "1-1"), ("symbol", "AMD")]:
        body = client.get(f"/api/journal/breakdowns?book=research&by={by}").json()
        assert "buckets" in body
        assert some_label in body["buckets"]
        assert set(body["buckets"][some_label]) == STAT_KEYS
        assert body["buckets"][some_label]["unit"] == "R"


def test_breakdowns_bad_by_is_422(tmp_path: Path) -> None:
    assert _client(tmp_path).get(
        "/api/journal/breakdowns?book=research&by=bogus").status_code == 422


def test_discipline_report_shape(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    _seed(url, [_pt("AMD", 2.0, exit_date=date(2026, 1, 5), exit_reason="target",
                    entry_price=100.0, low_water=98.0, high_water=112.0)])
    body = _client(tmp_path).get("/api/journal/discipline?book=research").json()
    assert set(body) == {"giveback_r", "stop_honored_rate", "avg_mae_before_win",
                         "n_closed", "n_with_excursion", "n_wins", "n_stopped",
                         "n_with_exit_reason"}
    assert body["n_closed"] == 1


def test_mistakes_rows_carry_stat(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    _seed(url, [_pt("AMD", -2.0, exit_date=date(2026, 1, 5))])
    with Session(get_engine(url)) as s:
        trade = s.query(PaperTrade).one()
        chased = add_tag(s, kind="mistake", name="chased")
        tag_trade(s, trade_id=trade.id, book="research", tag_id=chased.id, source="human")

    rows = _client(tmp_path).get("/api/journal/mistakes?book=research").json()
    assert len(rows) == 1
    assert set(rows[0]) == {"mistake", "n", "total_r", "stat"}
    assert rows[0]["mistake"] == "chased"
    assert rows[0]["total_r"] == -2.0
    assert set(rows[0]["stat"]) == STAT_KEYS


def test_notes_post_requires_header_then_get_returns_it(tmp_path: Path) -> None:
    client = _client(tmp_path)
    body = {"day": "2026-01-05", "kind": "premarket", "body": "watching semis"}
    assert client.post("/api/journal/notes", json=body).status_code == 403
    r = client.post("/api/journal/notes", json=body, headers=_HDR)
    assert r.status_code == 200
    created = r.json()
    assert created["source"] == "human"
    assert created["kind"] == "premarket"

    got = client.get("/api/journal/notes?day=2026-01-05").json()
    assert [n["body"] for n in got] == ["watching semis"]
    assert set(got[0]) == {"id", "day", "kind", "module", "body", "source", "created_at"}


def test_notes_bad_kind_is_422(tmp_path: Path) -> None:
    r = _client(tmp_path).post(
        "/api/journal/notes",
        json={"day": "2026-01-05", "kind": "bogus", "body": "x"}, headers=_HDR)
    assert r.status_code == 422


def test_tag_a_trade_requires_header_and_shows_up(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    _seed(url, [_pt("AMD", -2.0, exit_date=date(2026, 1, 5))])
    client = _client(tmp_path)
    tid = 1
    body = {"kind": "mistake", "name": "chased"}
    assert client.post(f"/api/journal/trades/research/{tid}/tags", json=body).status_code == 403
    r = client.post(f"/api/journal/trades/research/{tid}/tags", json=body, headers=_HDR)
    assert r.status_code == 200
    assert r.json()["source"] == "human"
    # the tag now rides the trade's record
    rec = client.get("/api/journal/records?book=research").json()[0]
    assert rec["tags"] == [{"name": "chased", "kind": "mistake", "source": "human"}]


def test_tag_unknown_trade_is_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    r = client.post("/api/journal/trades/research/999/tags",
                    json={"kind": "mistake", "name": "chased"}, headers=_HDR)
    assert r.status_code == 404


def test_records_shape(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    _seed(url, [_pt("AMD", 2.0, exit_date=date(2026, 1, 5))])
    rec = _client(tmp_path).get("/api/journal/records?book=research").json()
    assert len(rec) == 1
    assert set(rec[0]) == {"trade_id", "book", "module", "symbol", "direction",
                           "opened", "closed", "unit", "r", "tags", "theses"}
    assert rec[0]["module"] == "swing"
    assert rec[0]["unit"] == "R"
    assert rec[0]["direction"] == "long"
    assert rec[0]["r"] == 2.0


def test_journal_calendar_dead_db_is_503(tmp_path: Path) -> None:
    url = "sqlite:///Z:/definitely/nope/x.db"
    client = TestClient(create_app(url, edge_dir=tmp_path), raise_server_exceptions=False)
    assert client.get("/api/journal/calendar?book=research").status_code == 503


def test_change_token_watches_journal_tables(tmp_path: Path) -> None:
    """A new note/tag/thesis moves the SSE change token so other windows wake."""
    from swing_screener.cockpit.routers.events import _change_token
    from swing_screener.journal.repo import add_note

    url = _db_url(tmp_path)
    engine = get_engine(url)
    before = _change_token(engine, tmp_path)
    with Session(engine) as s:
        add_note(s, day=date(2026, 1, 5), kind="adhoc", body="x", source="human",
                 created_at=datetime(2026, 1, 5, 9, 0))
    after = _change_token(engine, tmp_path)
    assert before["journal_notes"] != after["journal_notes"]

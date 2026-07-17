"""GET /api/books/open -- the running forward book at trade granularity.

The contract under test: the loader slice is EXACTLY the book reflection grades
(open + research + baseline arm + default variant -- the arm x variant dedup),
the live grades share ``_live_grades`` with /api/positions, and degradation is
per row -- a quote miss is not an error, a raising fetch stamps class names
only, and one sick row never blanks the panel.
"""

from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import create_app
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine

# The row wire form -- a closed set, like test_api.py's STAT_KEYS.
ROW_KEYS = {"id", "ticker", "play_type", "strength", "conviction_tier",
            "opened_date", "age_days", "entry", "stop", "target", "risk",
            "last_close", "unrealized_r", "unrealized_pct", "to_stop_r",
            "to_target_r", "quote_error"}
BODY_KEYS = {"rows", "count", "as_of", "book_label"}


def _db_url(tmp_path: Path) -> str:
    # as_posix(): backslashes in a sqlite URL are asking for escape trouble.
    return f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"


def _client(
    tmp_path: Path, prices: dict[str, float] | None = None,
    fetch=None,
) -> tuple[TestClient, Engine]:
    """An app wired through the quote seam (fixed dict, misses absent -- the
    ``latest_closes`` failure shape) or a custom raising fetch; the broker
    factory answers None so the suite never touches a venue."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    fixed = dict(prices or {})
    app = create_app(url, edge_dir=tmp_path,
                     latest_closes_fn=fetch if fetch is not None
                     else (lambda tickers: fixed),
                     broker_factory=lambda: None)
    return TestClient(app), engine


def _open_trade(ticker: str, *, opened: date | None = None,
                account: str = "research", arm: str = "baseline",
                variant: str = "default", status: str = "open",
                would_surface: bool | None = None,
                entry: float | None = 100.0) -> PaperTrade:
    """An open forward-book row (entry 100 / stop 95 / target 110 / risk 5);
    the keyword seams flip it into each decoy the endpoint must exclude."""
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8,
        rank=1, account=account, play_type="reversal", strength="confirmed",
        conviction_tier="premium", arm=arm, variant=variant,
        would_surface=would_surface, fill_status="filled", status=status,
        entry_price=entry, entry_date=opened, opened_date=opened,
        stop=95.0, target=110.0, risk=5.0,
    )


def test_open_book_slice_ordering_and_live_math(tmp_path: Path) -> None:
    """Only OPEN research rows at (baseline, default) appear -- other arms,
    other variants, other accounts, closed and pending rows are all fenced out
    (the arm x variant fill multiplication dedup). Rows order opened_date DESC
    then ticker ASC; the live grades are _live_grades' math verbatim, with a
    breached stop reading a NEGATIVE to_stop_r."""
    recent, older = date.today() - timedelta(days=3), date.today() - timedelta(days=10)
    client, engine = _client(
        tmp_path, {"NEWA": 104.0, "NEWB": 94.0, "OLDR": 104.0})
    with Session(engine) as s:
        s.add(_open_trade("NEWB", opened=recent))
        s.add(_open_trade("NEWA", opened=recent))
        s.add(_open_trade("OLDR", opened=older))
        # The decoys: each flips exactly one filter key off the graded slice.
        s.add(_open_trade("ARMX", opened=recent, arm="partial33_cond"))
        s.add(_open_trade("VARX", opened=recent, variant="tight_freshness"))
        s.add(_open_trade("PAPX", opened=recent, account="paper"))
        s.add(_open_trade("LIVX", opened=recent, account="live"))
        s.add(_open_trade("CLSX", opened=recent, status="closed"))
        s.add(_open_trade("PNDX", opened=recent, status="pending"))
        s.commit()
    r = client.get("/api/books/open")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == BODY_KEYS
    assert body["count"] == 3
    assert datetime.fromisoformat(body["as_of"])
    assert body["book_label"] == (
        "research · baseline/default — the forward shadow book")
    assert [row["ticker"] for row in body["rows"]] == ["NEWA", "NEWB", "OLDR"]
    for row in body["rows"]:
        assert set(row) == ROW_KEYS
        assert row["play_type"] == "reversal"
        assert row["strength"] == "confirmed"
        assert row["conviction_tier"] == "premium"
        assert row["quote_error"] is None

    newa = body["rows"][0]  # priced 104 over entry 100 / stop 95 / target 110 / risk 5
    assert newa["opened_date"] == recent.isoformat()
    assert newa["age_days"] == 3
    assert newa["entry"] == 100.0 and newa["stop"] == 95.0
    assert newa["target"] == 110.0 and newa["risk"] == 5.0
    assert newa["last_close"] == 104.0
    assert newa["unrealized_r"] == pytest.approx(0.8)     # (104-100)/5
    assert newa["unrealized_pct"] == pytest.approx(0.04)  # FRACTION, not 4.0
    assert newa["to_stop_r"] == pytest.approx(1.8)        # (104-95)/5
    assert newa["to_target_r"] == pytest.approx(1.2)      # (110-104)/5

    newb = body["rows"][1]  # priced 94: below the stop -- the signs must say so
    assert newb["unrealized_r"] == pytest.approx(-1.2)
    assert newb["to_stop_r"] == pytest.approx(-0.2)   # negative = stop breached
    assert newb["to_target_r"] == pytest.approx(3.2)

    oldr = body["rows"][2]
    assert oldr["age_days"] == 10


def test_open_book_gold_facet_and_label(tmp_path: Path) -> None:
    """The gold facet is settlement's facet_filter verbatim: would_surface must
    be TRUTHY -- False and None (legacy/replay) both stay research-only. The
    label says so; a bogus facet is FastAPI's 422 via the Literal."""
    opened = date.today() - timedelta(days=1)
    client, engine = _client(tmp_path, {"GOLD": 104.0, "NOPE": 104.0, "LEGC": 104.0})
    with Session(engine) as s:
        s.add(_open_trade("GOLD", opened=opened, would_surface=True))
        s.add(_open_trade("NOPE", opened=opened, would_surface=False))
        s.add(_open_trade("LEGC", opened=opened, would_surface=None))
        s.commit()
    research = client.get("/api/books/open").json()
    assert {row["ticker"] for row in research["rows"]} == {"GOLD", "NOPE", "LEGC"}

    gold = client.get("/api/books/open?facet=gold").json()
    assert [row["ticker"] for row in gold["rows"]] == ["GOLD"]
    assert gold["count"] == 1
    assert "gold" in gold["book_label"]
    assert "research" in gold["book_label"]  # the slice is still stated

    assert client.get("/api/books/open?facet=bogus").status_code == 422


def test_open_book_quote_miss_and_sick_row_degrade_per_row(tmp_path: Path) -> None:
    """A quote MISS (absent from the cache dict) is NOT an error: last_close and
    the live fields null, quote_error null. A priced row whose entry is
    pathologically null keeps last_close (the quote was fine) with null live
    fields. Healthy rows in the same response stay fully graded."""
    opened = date.today() - timedelta(days=2)
    client, engine = _client(tmp_path, {"GOOD": 104.0, "NOEN": 104.0})
    with Session(engine) as s:
        s.add(_open_trade("GOOD", opened=opened))
        s.add(_open_trade("MISS", opened=opened))          # no quote for it
        s.add(_open_trade("NOEN", opened=opened, entry=None))
        s.add(_open_trade("NODT"))  # legacy: no opened_date at all
        s.commit()
    r = client.get("/api/books/open")
    assert r.status_code == 200
    body = r.json()
    rows = {row["ticker"]: row for row in body["rows"]}
    assert set(rows) == {"GOOD", "MISS", "NOEN", "NODT"}  # every row KEPT

    miss = rows["MISS"]
    assert miss["last_close"] is None and miss["quote_error"] is None
    assert miss["unrealized_r"] is None and miss["to_stop_r"] is None

    noen = rows["NOEN"]  # the quote itself was fine and still shows
    assert noen["last_close"] == 104.0 and noen["entry"] is None
    assert noen["unrealized_r"] is None and noen["unrealized_pct"] is None

    nodt = rows["NODT"]  # legacy null opened_date: honest nulls, sorts oldest
    assert nodt["opened_date"] is None and nodt["age_days"] is None
    assert body["rows"][-1]["ticker"] == "NODT"

    assert rows["GOOD"]["unrealized_r"] == pytest.approx(0.8)


def test_open_book_raising_fetch_stamps_class_name_only(tmp_path: Path) -> None:
    """A RAISING quote fetch (a seam-provided fetch may raise; the real one never
    does) degrades EVERY row instead of 503ing the panel: live fields null,
    quote_error the exception CLASS NAME only -- the message can embed hosts or
    paths and must never reach the wire -- and as_of null."""
    def boom(tickers: list[str]) -> dict[str, float]:
        raise RuntimeError("secret-host.internal:8443 credentials leaked")

    client, engine = _client(tmp_path, fetch=boom)
    with Session(engine) as s:
        s.add(_open_trade("AAA", opened=date.today() - timedelta(days=1)))
        s.add(_open_trade("BBB", opened=date.today() - timedelta(days=1)))
        s.commit()
    r = client.get("/api/books/open")
    assert r.status_code == 200
    body = r.json()
    assert body["as_of"] is None
    assert body["count"] == 2
    for row in body["rows"]:
        assert row["quote_error"] == "RuntimeError"  # class name, nothing else
        assert row["last_close"] is None and row["unrealized_r"] is None
    assert "secret-host" not in r.text  # the message never reaches the wire


def test_open_book_empty_book_is_a_normal_state(tmp_path: Path) -> None:
    """No open forward-book trades -> honest empties, never an error: rows [],
    count 0, the label still stated, and as_of still a timestamp (the cache's
    nothing-cached-means-nothing-stale rule)."""
    client, _engine = _client(tmp_path)
    body = client.get("/api/books/open").json()
    assert body["rows"] == [] and body["count"] == 0
    assert datetime.fromisoformat(body["as_of"])
    assert "forward shadow book" in body["book_label"]

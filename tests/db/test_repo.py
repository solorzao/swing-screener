from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import PaperTrade, Signal
from swing_screener.db.session import get_engine


def _signal(ticker="AAPL", rank=1, score=0.8):
    return Signal(
        run_date=date(2024, 1, 2), ticker=ticker, timeframe="1d", horizon="medium",
        score=score, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
        entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
    )


def test_save_and_latest_signals_ordered_by_rank():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_signals(s, [_signal("MSFT", rank=2, score=0.6), _signal("AAPL", rank=1, score=0.9)])
        got = repo.latest_signals(s, date(2024, 1, 2))
        assert [x.ticker for x in got] == ["AAPL", "MSFT"]  # ascending rank


def test_open_paper_trades_and_record_exit():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [PaperTrade(
            ticker="AAPL", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
            fill_status="filled", stop=95.0, target=110.0, risk=5.0, status="open",
        )])
        opened = repo.load_open_paper_trades(s)
        assert len(opened) == 1 and opened[0].ticker == "AAPL"

        ev = repo.record_exit_event(s, is_paper=True, trade_id=opened[0].id,
                                    tier="hard", reason="stop", message="stopped out",
                                    created_date=date(2024, 1, 5))
        assert ev.id is not None and ev.reason == "stop"


def test_list_universe_orders_and_filters_by_ticker():
    from sqlalchemy.orm import Session
    from swing_screener.db.models import Universe
    from swing_screener.db.session import get_engine
    from swing_screener.db import repo
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([Universe(ticker="NVDA", name="Nvidia"),
                   Universe(ticker="AMD", name="Advanced Micro")])
        s.commit()
        assert [u.ticker for u in repo.list_universe(s)] == ["AMD", "NVDA"]   # ordered by ticker
        assert [u.ticker for u in repo.list_universe(s, search="nv")] == ["NVDA"]  # case-insensitive


def test_list_universe_escapes_like_wildcards():
    # `_` is a LIKE wildcard; the search must treat it as a LITERAL underscore so
    # search="_" matches only tickers that literally contain "_", not every row.
    from sqlalchemy.orm import Session

    from swing_screener.db import repo
    from swing_screener.db.models import Universe
    from swing_screener.db.session import get_engine
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([Universe(ticker="ABC", name="No underscore"),
                   Universe(ticker="BRK_B", name="Has underscore")])
        s.commit()
        assert [u.ticker for u in repo.list_universe(s, search="_")] == ["BRK_B"]
        # `%` must also be literal: it matches nothing here, not everything.
        assert repo.list_universe(s, search="%") == []


def test_sync_universe_mirrors_seed_and_preserves_metrics():
    from sqlalchemy.orm import Session
    from swing_screener.data.universe import UniverseEntry
    from swing_screener.db.models import Universe
    from swing_screener.db.session import get_engine
    from swing_screener.db import repo
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([
            Universe(ticker="OLD", name="Old Co", exchange="NYSE", market_cap=9.0),
            Universe(ticker="KEEP", name="Old Name", exchange="NYSE", market_cap=5.0,
                     avg_dollar_volume=7.0),
        ])
        s.commit()
        repo.sync_universe(s, [
            UniverseEntry(ticker="KEEP", name="New Name", exchange="NASDAQ"),
            UniverseEntry(ticker="NEW", name="New Co", exchange="NYSE"),
        ])
        rows = {u.ticker: u for u in repo.list_universe(s)}
    assert set(rows) == {"KEEP", "NEW"}            # OLD deleted, NEW added
    assert rows["KEEP"].name == "New Name"         # name upserted
    assert rows["KEEP"].exchange == "NASDAQ"
    assert rows["KEEP"].market_cap == 5.0          # metrics preserved
    assert rows["KEEP"].avg_dollar_volume == 7.0
    assert rows["NEW"].market_cap is None


def test_apply_universe_metrics_skips_none_and_unknown():
    from sqlalchemy.orm import Session
    from swing_screener.db.models import Universe
    from swing_screener.db.session import get_engine
    from swing_screener.db import repo
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(Universe(ticker="A", name="A", exchange="NYSE", market_cap=1.0,
                       avg_dollar_volume=2.0))
        s.commit()
        repo.apply_universe_metrics(s, {
            "A": {"market_cap": 50.0, "avg_dollar_volume": None},  # None skipped
            "MISSING": {"market_cap": 99.0, "avg_dollar_volume": 9.0},  # unknown ignored
        })
        rows = {u.ticker: u for u in repo.list_universe(s)}
    assert rows["A"].market_cap == 50.0
    assert rows["A"].avg_dollar_volume == 2.0  # preserved (None was skipped)
    assert "MISSING" not in rows


def test_list_email_log_newest_first():
    from datetime import datetime
    from sqlalchemy.orm import Session
    from swing_screener.db.models import EmailLog
    from swing_screener.db.session import get_engine
    from swing_screener.db import repo
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([EmailLog(sent_at=datetime(2026, 1, 1), kind="daily", subject="old"),
                   EmailLog(sent_at=datetime(2026, 1, 2), kind="weekly", subject="new")])
        s.commit()
        assert [e.subject for e in repo.list_email_log(s)] == ["new", "old"]


def test_create_and_list_analysis_requests():
    from datetime import datetime
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.create_analysis_request(s, ticker="AMD",
                                     requested_at=datetime(2026, 6, 16, 9, 0))
        repo.create_analysis_request(s, ticker="NVDA",
                                     requested_at=datetime(2026, 6, 16, 10, 0),
                                     recipient="me@example.com")
        rows = repo.list_analysis_requests(s)
        assert [r.ticker for r in rows] == ["NVDA", "AMD"]  # newest requested_at first
        assert rows[0].recipient == "me@example.com"
        assert rows[0].status == "queued"
        # round-trip fetch by id
        assert repo.get_analysis_request(s, rows[0].id).ticker == "NVDA"
        assert repo.get_analysis_request(s, 99999) is None


def test_claim_queued_is_atomic_and_idempotent():
    from datetime import datetime
    engine = get_engine("sqlite:///:memory:")
    now = datetime(2026, 6, 16, 12, 0)
    with Session(engine) as s:
        repo.create_analysis_request(s, ticker="AMD",
                                     requested_at=datetime(2026, 6, 16, 9, 0))
        repo.create_analysis_request(s, ticker="NVDA",
                                     requested_at=datetime(2026, 6, 16, 10, 0))
        claimed = repo.claim_queued_requests(s, now=now)
        assert len(claimed) == 2
        assert all(r.status == "running" for r in claimed)
        assert all(r.started_at == now for r in claimed)
        # oldest requested_at is claimed first
        assert [r.ticker for r in claimed] == ["AMD", "NVDA"]
        # a second claim finds nothing left queued
        assert repo.claim_queued_requests(s, now=now) == []


def test_complete_and_fail():
    from datetime import datetime
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        ok = repo.create_analysis_request(s, ticker="AMD",
                                          requested_at=datetime(2026, 6, 16, 9, 0))
        bad = repo.create_analysis_request(s, ticker="NVDA",
                                           requested_at=datetime(2026, 6, 16, 10, 0))
        repo.complete_analysis_request(
            s, ok.id, summary="looks bullish", pdf_blob_key="reports/amd.pdf",
            chart_blob_keys="c1,c2", finished_at=datetime(2026, 6, 16, 12, 5))
        repo.fail_analysis_request(
            s, bad.id, error="no data", finished_at=datetime(2026, 6, 16, 12, 6))
        done = repo.get_analysis_request(s, ok.id)
        assert done.status == "done"
        assert done.summary == "looks bullish"
        assert done.pdf_blob_key == "reports/amd.pdf"
        assert done.chart_blob_keys == "c1,c2"
        assert done.finished_at == datetime(2026, 6, 16, 12, 5)
        failed = repo.get_analysis_request(s, bad.id)
        assert failed.status == "failed"
        assert failed.error == "no data"
        assert failed.finished_at == datetime(2026, 6, 16, 12, 6)

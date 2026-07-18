from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import PaperTrade, ReversalFunnel, Signal, Universe
from swing_screener.db.session import get_engine


def _signal(ticker="AAPL", rank=1, score=0.8):
    return Signal(
        run_date=date(2024, 1, 2), ticker=ticker, timeframe="1d", horizon="medium",
        score=score, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
        entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
    )


def _sig_on(ticker, run_date, first_seen):
    return Signal(
        run_date=run_date, ticker=ticker, timeframe="1d", horizon="medium",
        play_type="continuation", score=0.8, rank=1, trigger_close=100.0, atr=4.0,
        rsi=55.0, entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
        first_seen_date=first_seen,
    )


def test_save_and_latest_signals_ordered_by_rank():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_signals(s, [_signal("MSFT", rank=2, score=0.6), _signal("AAPL", rank=1, score=0.9)])
        got = repo.latest_signals(s, date(2024, 1, 2))
        assert [x.ticker for x in got] == ["AAPL", "MSFT"]  # ascending rank


def test_prior_first_seen_survives_a_one_run_gap():
    """A setup that flickers (fires, skips a run, fires again) keeps its streak start:
    prior_first_seen looks back over recent runs, not just the immediately-prior one, so a
    one-run gap does not reset first_seen (which would defeat the repeat cooldown)."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        # Apr 1: AAPL's streak starts. Apr 2: AAPL absent (only MSFT fires) -- the gap.
        s.add_all([
            _sig_on("AAPL", date(2024, 4, 1), date(2024, 4, 1)),
            _sig_on("MSFT", date(2024, 4, 2), date(2024, 4, 2)),
        ])
        s.commit()
        seen = repo.prior_first_seen(s, date(2024, 4, 3))
    assert seen[("AAPL", "1d", "continuation")] == date(2024, 4, 1)


def test_prior_first_seen_resets_after_absence_beyond_lookback():
    """Outside the lookback window a setup is a fresh streak: a name absent for more runs
    than the window does not inherit a stale first_seen."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        # AAPL last fired Apr 1, then two runs without it. With lookback_runs=2 the Apr 1
        # run is outside the window, so AAPL is not carried forward.
        s.add_all([
            _sig_on("AAPL", date(2024, 4, 1), date(2024, 4, 1)),
            _sig_on("MSFT", date(2024, 4, 2), date(2024, 4, 2)),
            _sig_on("MSFT", date(2024, 4, 3), date(2024, 4, 2)),
        ])
        s.commit()
        seen = repo.prior_first_seen(s, date(2024, 4, 4), lookback_runs=2)
    assert ("AAPL", "1d", "continuation") not in seen


def test_apply_universe_metrics_writes_sector_and_skips_none():
    """sector is written when present and SKIPPED when None, so a transient yfinance
    failure (None) preserves the prior value rather than wiping it."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(Universe(ticker="AAPL", name="Apple", sector="Information Technology"))
        s.commit()
        # None sector must NOT overwrite the existing value (sticky on fetch failure).
        repo.apply_universe_metrics(s, {"AAPL": {"sector": None, "market_cap": 5.0}})
        row = s.get(Universe, "AAPL")
        assert row is not None
        assert row.sector == "Information Technology" and row.market_cap == 5.0
        # a real sector updates it.
        repo.apply_universe_metrics(s, {"AAPL": {"sector": "Energy"}})
        assert s.get(Universe, "AAPL").sector == "Energy"


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
        # Read-back is self-identifying: every returned row is running AND stamped
        # with THIS call's `now`, so a concurrent replica's claim can't leak in.
        assert all(r.status == "running" for r in claimed)
        assert all(r.started_at == now for r in claimed)
        # oldest requested_at is claimed first
        assert [r.ticker for r in claimed] == ["AMD", "NVDA"]
        # a second claim finds nothing left queued
        assert repo.claim_queued_requests(s, now=now) == []


def test_claim_stamps_microsecond_free_token():
    """The claim must truncate its token to whole seconds BEFORE stamping.

    Callers pass full-precision ``datetime.now(UTC)``; SQL Server's DATETIME stores
    at 1/300s ticks (rounded), so a microsecond-bearing stamp never equals the
    full-precision bind parameter in the ``started_at == now`` read-back -- the
    claim returns [] on Azure SQL and rows stall 'running' forever. Whole seconds
    are exactly representable in DATETIME, so microsecond-free STORAGE is the
    portable property that makes the equality hold on every backend. (SQLite
    round-trips microseconds exactly, which is why it cannot catch the rounding
    itself -- so we pin the stored value instead.)
    """
    from datetime import datetime
    engine = get_engine("sqlite:///:memory:")
    now = datetime(2026, 7, 17, 12, 0, 0, 123456)
    with Session(engine) as s:
        req = repo.create_analysis_request(s, ticker="AMD",
                                           requested_at=datetime(2026, 7, 17, 9, 0))
        req_id = req.id
        claimed = repo.claim_queued_requests(s, now=now)
        assert [r.ticker for r in claimed] == ["AMD"]
        assert claimed[0].started_at.microsecond == 0
    # Fresh session (not the identity-mapped object above): what actually got STORED
    # is microsecond-free, so DATETIME rounding is a no-op.
    with Session(engine) as s2:
        stored = repo.get_analysis_request(s2, req_id)
        assert stored is not None
        assert stored.started_at == datetime(2026, 7, 17, 12, 0, 0)


def test_sequential_claims_keep_their_own_rows():
    """Race-safety survives truncation: each claim stamps its own whole-second token
    and only reads back rows carrying THAT token, so two workers claiming at
    different times never return each other's rows."""
    from datetime import datetime
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.create_analysis_request(s, ticker="AMD",
                                     requested_at=datetime(2026, 7, 17, 9, 0))
        repo.create_analysis_request(s, ticker="NVDA",
                                     requested_at=datetime(2026, 7, 17, 10, 0))
        first = repo.claim_queued_requests(s, now=datetime(2026, 7, 17, 12, 0, 0), limit=1)
        second = repo.claim_queued_requests(s, now=datetime(2026, 7, 17, 12, 0, 1), limit=1)
        assert [r.ticker for r in first] == ["AMD"]
        assert [r.ticker for r in second] == ["NVDA"]
        assert first[0].started_at == datetime(2026, 7, 17, 12, 0, 0)
        assert second[0].started_at == datetime(2026, 7, 17, 12, 0, 1)


def test_requeue_stale_running():
    from datetime import datetime
    engine = get_engine("sqlite:///:memory:")
    cutoff = datetime(2026, 6, 16, 12, 0)
    with Session(engine) as s:
        stale = repo.create_analysis_request(s, ticker="AMD",
                                             requested_at=datetime(2026, 6, 16, 8, 0))
        fresh = repo.create_analysis_request(s, ticker="NVDA",
                                             requested_at=datetime(2026, 6, 16, 8, 5))
        # Both are mid-flight ('running'); the stale one started long before the cutoff,
        # the fresh one just after it.
        stale.status = "running"
        stale.started_at = datetime(2026, 6, 16, 11, 0)   # before cutoff -> stale
        fresh.status = "running"
        fresh.started_at = datetime(2026, 6, 16, 12, 30)  # after cutoff -> untouched
        s.commit()

        n = repo.requeue_stale_running(s, cutoff=cutoff)
        assert n == 1
        # the stale row is back in the queue with its start cleared
        stale_row = repo.get_analysis_request(s, stale.id)
        assert stale_row.status == "queued"
        assert stale_row.started_at is None
        # the recent row is left running, untouched
        fresh_row = repo.get_analysis_request(s, fresh.id)
        assert fresh_row.status == "running"
        assert fresh_row.started_at == datetime(2026, 6, 16, 12, 30)


def _save_funnel(s: Session, run_date: date, **overrides: object) -> None:
    kwargs: dict[str, object] = dict(
        detected=31, confirmed=12, fresh=9, actionable=7, surfaced=5,
        overflow_tickers="CRM,WDAY", pool_n=20, confirmed_only=True,
        premium_only=False, already_ran_checked=True)
    kwargs.update(overrides)
    repo.save_reversal_funnel(s, run_date=run_date, **kwargs)  # type: ignore[arg-type]


def test_save_reversal_funnel_idempotent_per_run_date() -> None:
    """A forced digest resend re-executes the funnel write for the same run_date: one
    row must remain, carrying the SECOND write's values (delete-then-insert, the
    delete_signals_for rewrite posture)."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        _save_funnel(s, date(2026, 7, 9))
        _save_funnel(s, date(2026, 7, 9), detected=30, confirmed=11, fresh=8,
                     actionable=6, surfaced=4, overflow_tickers="",
                     already_ran_checked=False)
        rows = list(s.scalars(select(ReversalFunnel)))
        assert len(rows) == 1
        row = rows[0]
        assert (row.detected, row.confirmed, row.fresh, row.actionable,
                row.surfaced) == (30, 11, 8, 6, 4)
        assert row.overflow_tickers == ""
        assert row.pool_n == 20
        assert row.confirmed_only is True
        assert row.premium_only is False
        assert row.already_ran_checked is False
        assert row.created_at is not None  # stamped at save time


def test_latest_reversal_funnel_returns_newest() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        assert repo.latest_reversal_funnel(s) is None  # empty table
        _save_funnel(s, date(2026, 7, 8), detected=5)
        _save_funnel(s, date(2026, 7, 9), detected=9)
        got = repo.latest_reversal_funnel(s)
        assert got is not None
        assert got.run_date == date(2026, 7, 9)  # newest run_date wins
        assert got.detected == 9


def test_save_reversal_funnel_survives_concurrent_replica_race(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent same-run_date runs (the Sunday double-fire that bit market_run
    in 2026-06) both pass the delete and both insert; the loser's commit hits the
    unique index AFTER real work is done. Losing is benign -- the winner's row was
    computed from the same DB state -- so no exception may escape and the winner's
    row must survive. Simulated by making the loser's commit raise IntegrityError."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        _save_funnel(s, date(2026, 7, 9))  # the winner's row (detected=31)

        real_commit = s.commit
        state = {"raised": False}

        def losing_commit() -> None:
            if not state["raised"]:
                state["raised"] = True
                raise IntegrityError("stmt", None, Exception("UNIQUE constraint failed"))
            real_commit()

        monkeypatch.setattr(s, "commit", losing_commit)
        got = repo.save_reversal_funnel(  # the loser: same run_date, different counts
            s, run_date=date(2026, 7, 9), detected=99, confirmed=99, fresh=99,
            actionable=99, surfaced=99, overflow_tickers="XXX", pool_n=20,
            confirmed_only=True, premium_only=False, already_ran_checked=True)

        assert got.detected == 31  # the loser got the WINNER's row back, not its own
        rows = list(s.scalars(select(ReversalFunnel)))
        assert len(rows) == 1
        assert rows[0].detected == 31  # the winner's row survived the race


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

"""Tests for the on-demand analysis worker (notify.ondemand). All OFFLINE.

yfinance, Anthropic, and email are all faked/monkeypatched so no network is hit:
``_fetch_all_timeframes`` returns a canned firing daily frame, ``analyze_ticker_deep``
is stubbed to a deterministic tuple, and a fake sender records ``send`` calls. We
assert one request runs end-to-end (done + summary + pdf key + exactly one email
with a PDF attachment), idempotency (a second pass is a no-op), and per-request
failure isolation (a raising fetch ends the row ``failed`` without aborting).
"""

from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.session import get_engine
from swing_screener.notify import ondemand
from swing_screener.notify.analysis import TickerAnalysis, Usage
from swing_screener.settings import load_settings


def _firing(bars):
    """A daily frame that fires a continuation setup (mirrors tests/pipeline/test_run.py)."""
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})
    return bars(rows)


class _FakeSender:
    """A callable transport (same signature as resolve_sender's) that records calls."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *, to, subject, text, html, attachments):
        self.calls.append(
            {"to": to, "subject": subject, "text": text, "html": html,
             "attachments": list(attachments)}
        )


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Settings with all dirs under tmp_path and a sqlite DB; recipient via env."""
    monkeypatch.setenv("SWING_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SWING_CHART_DIR", str(tmp_path / "charts"))
    monkeypatch.setenv("SWING_PDF_DIR", str(tmp_path / "pdfs"))
    monkeypatch.setenv("SWING_DB_URL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    monkeypatch.setenv("DIGEST_TO", "trader@example.com")
    return load_settings()


@pytest.fixture
def _patch_fetch(monkeypatch, bars):
    """Default: _fetch_all_timeframes yields a firing daily frame for any ticker."""
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing(bars)}

    monkeypatch.setattr(ondemand, "_fetch_all_timeframes", fake_fetch)


@pytest.fixture
def _patch_analyze(monkeypatch):
    """Keep the worker test focused: stub the Opus call to a canned TickerAnalysis.

    Carries a Usage (1000 in / 500 out at opus-4-8 list price = $0.0175) so the
    est_cost persistence tests can assert the exact figure."""
    monkeypatch.setattr(
        ondemand, "analyze_ticker_deep",
        lambda report, **kw: TickerAnalysis(
            summary="S", analysis_text="1d: up", is_deep=True,
            usage=Usage(input_tokens=1000, output_tokens=500, web_searches=0,
                        est_cost_usd=0.0175)),
    )


def test_process_pending_completes_and_emails(settings, _patch_fetch, _patch_analyze):
    engine = get_engine(settings.db_url)
    sender = _FakeSender()
    with Session(engine) as s:
        req = repo.create_analysis_request(
            s, ticker="AAPL", requested_at=datetime(2026, 6, 16, 9, 0))  # noqa: DTZ001 -- naive test fixture
        n = ondemand.process_pending(
            s, settings=settings, now=datetime(2026, 6, 16, 12, 0),  # noqa: DTZ001 -- naive test fixture
            today=date(2026, 6, 16), sender=sender)
        assert n == 1
        done = repo.get_analysis_request(s, req.id)
        assert done.status == "done"
        assert done.summary == "S"
        assert done.pdf_blob_key  # non-empty PDF key/path
    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert call["to"] == "trader@example.com"
    assert len(call["attachments"]) == 1
    assert str(call["attachments"][0]).endswith(".pdf")


def test_process_pending_persists_est_cost(settings, _patch_fetch, _patch_analyze):
    """E3c: the on-demand path is UNCAPPED (4 chart images + web search per request,
    crash-requeue can re-bill) -- the completed row must carry the call's estimated
    spend so the cost is at least visible."""
    engine = get_engine(settings.db_url)
    with Session(engine) as s:
        req = repo.create_analysis_request(
            s, ticker="AAPL", requested_at=datetime(2026, 6, 16, 9, 0))  # noqa: DTZ001 -- naive test fixture
        ondemand.process_pending(
            s, settings=settings, now=datetime(2026, 6, 16, 12, 0),  # noqa: DTZ001 -- naive test fixture
            today=date(2026, 6, 16), sender=_FakeSender())
        done = repo.get_analysis_request(s, req.id)
        assert done.status == "done"
        assert done.est_cost_usd == pytest.approx(0.0175)


def test_process_pending_est_cost_null_on_fallback(settings, _patch_fetch, monkeypatch):
    """A deterministic-fallback analysis (usage None: no billed call captured)
    leaves est_cost_usd NULL -- an honest 'unknown', never a fake $0."""
    monkeypatch.setattr(
        ondemand, "analyze_ticker_deep",
        lambda report, **kw: TickerAnalysis(
            summary="S", analysis_text="1d: up", is_deep=False, usage=None),
    )
    engine = get_engine(settings.db_url)
    with Session(engine) as s:
        req = repo.create_analysis_request(
            s, ticker="AAPL", requested_at=datetime(2026, 6, 16, 9, 0))  # noqa: DTZ001 -- naive test fixture
        ondemand.process_pending(
            s, settings=settings, now=datetime(2026, 6, 16, 12, 0),  # noqa: DTZ001 -- naive test fixture
            today=date(2026, 6, 16), sender=_FakeSender())
        done = repo.get_analysis_request(s, req.id)
        assert done.status == "done"
        assert done.est_cost_usd is None


def test_process_pending_is_idempotent(settings, _patch_fetch, _patch_analyze):
    engine = get_engine(settings.db_url)
    sender = _FakeSender()
    with Session(engine) as s:
        repo.create_analysis_request(
            s, ticker="AAPL", requested_at=datetime(2026, 6, 16, 9, 0))  # noqa: DTZ001 -- naive test fixture
        ondemand.process_pending(
            s, settings=settings, now=datetime(2026, 6, 16, 12, 0),  # noqa: DTZ001 -- naive test fixture
            today=date(2026, 6, 16), sender=sender)
        # second pass: no queued rows remain, so nothing processed and no new email
        n2 = ondemand.process_pending(
            s, settings=settings, now=datetime(2026, 6, 16, 13, 0),  # noqa: DTZ001 -- naive test fixture
            today=date(2026, 6, 16), sender=sender)
    assert n2 == 0
    assert len(sender.calls) == 1  # still exactly one email


def test_process_one_isolates_failure(settings, monkeypatch):
    def boom(ticker, *, cache_dir, today, cfg):
        raise RuntimeError("fetch exploded https://secret-host.example/key=abc")

    monkeypatch.setattr(ondemand, "_fetch_all_timeframes", boom)
    engine = get_engine(settings.db_url)
    sender = _FakeSender()
    with Session(engine) as s:
        req = repo.create_analysis_request(
            s, ticker="AAPL", requested_at=datetime(2026, 6, 16, 9, 0))  # noqa: DTZ001 -- naive test fixture
        # never raises, despite the fetch blowing up
        n = ondemand.process_pending(
            s, settings=settings, now=datetime(2026, 6, 16, 12, 0),  # noqa: DTZ001 -- naive test fixture
            today=date(2026, 6, 16), sender=sender)
        assert n == 1
        failed = repo.get_analysis_request(s, req.id)
        assert failed.status == "failed"
        # Leak posture: the stored error reaches the cockpit wire -- exception
        # CLASS only; the message (hosts/URLs/keys) belongs in the log.
        assert failed.error == "error (RuntimeError)"
        assert "secret-host" not in failed.error
    assert sender.calls == []  # no email on failure


def test_process_one_no_data_fails_cleanly(settings, monkeypatch):
    monkeypatch.setattr(
        ondemand, "_fetch_all_timeframes",
        lambda ticker, *, cache_dir, today, cfg: {})
    engine = get_engine(settings.db_url)
    sender = _FakeSender()
    with Session(engine) as s:
        req = repo.create_analysis_request(
            s, ticker="ZZZ", requested_at=datetime(2026, 6, 16, 9, 0))  # noqa: DTZ001 -- naive test fixture
        ondemand.process_pending(
            s, settings=settings, now=datetime(2026, 6, 16, 12, 0),  # noqa: DTZ001 -- naive test fixture
            today=date(2026, 6, 16), sender=sender)
        failed = repo.get_analysis_request(s, req.id)
        assert failed.status == "failed"
        assert "no data" in (failed.error or "")
    assert sender.calls == []


def test_process_pending_requeues_stale_running(settings, _patch_fetch, _patch_analyze):
    """A request stuck 'running' from a dead worker is requeued and re-processed."""
    engine = get_engine(settings.db_url)
    sender = _FakeSender()
    with Session(engine) as s:
        req = repo.create_analysis_request(
            s, ticker="AAPL", requested_at=datetime(2026, 6, 16, 9, 0))  # noqa: DTZ001 -- naive test fixture
        # Simulate a crashed worker: row left 'running', started_at well over the
        # 30-min stale window before `now`.
        req.status = "running"
        req.started_at = datetime(2026, 6, 16, 10, 0)  # noqa: DTZ001 -- naive test fixture
        s.commit()

        n = ondemand.process_pending(
            s, settings=settings, now=datetime(2026, 6, 16, 12, 0),  # noqa: DTZ001 -- naive test fixture
            today=date(2026, 6, 16), sender=sender)
        assert n == 1  # requeued, then claimed and processed
        done = repo.get_analysis_request(s, req.id)
        assert done.status == "done"
    assert len(sender.calls) == 1


def test_email_logged_for_dedup(settings, _patch_fetch, _patch_analyze):
    """A done request leaves exactly one ondemand EmailLog row keyed on its id."""
    engine = get_engine(settings.db_url)
    sender = _FakeSender()
    with Session(engine) as s:
        req = repo.create_analysis_request(
            s, ticker="AAPL", requested_at=datetime(2026, 6, 16, 9, 0))  # noqa: DTZ001 -- naive test fixture
        ondemand.process_pending(
            s, settings=settings, now=datetime(2026, 6, 16, 12, 0),  # noqa: DTZ001 -- naive test fixture
            today=date(2026, 6, 16), sender=sender)
        logs = [
            e for e in repo.list_email_log(s)
            if e.kind == "ondemand" and e.alert_key == str(req.id)
        ]
    assert len(logs) == 1


def test_email_dedup_survives_a_cross_midnight_retry(settings, _patch_fetch, _patch_analyze):
    """The idempotency key rides the REQUEST's date, not the worker's today: a row
    that crashed after its email (stuck 'running', requeued) and retried past
    midnight must NOT email twice (2026-07-17 audit M4b)."""
    engine = get_engine(settings.db_url)
    sender = _FakeSender()
    with Session(engine) as s:
        req = repo.create_analysis_request(
            s, ticker="AAPL", requested_at=datetime(2026, 6, 16, 23, 50))  # noqa: DTZ001 -- naive test fixture
        ondemand.process_pending(
            s, settings=settings, now=datetime(2026, 6, 16, 23, 55),  # noqa: DTZ001 -- naive test fixture
            today=date(2026, 6, 16), sender=sender)
        assert len(sender.calls) == 1
        # Simulate the crash window: the email committed but complete_analysis_request
        # never ran -- the row is requeued for the next pass.
        row = repo.get_analysis_request(s, req.id)
        row.status = "queued"
        row.started_at = None
        row.finished_at = None
        s.commit()
        # The retry lands after midnight.
        ondemand.process_pending(
            s, settings=settings, now=datetime(2026, 6, 17, 0, 10),  # noqa: DTZ001 -- naive test fixture
            today=date(2026, 6, 17), sender=sender)
    assert len(sender.calls) == 1  # deduped on the request's own date, not today

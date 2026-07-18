"""The TICKER LAB router: the on-demand study endpoint (payload shape, 422s,
the upstream-503 class-name-only leak posture, genuine-bug-500), the deep-
analysis queue (403 without the header, queue + in-process drain seam), the
default worker end-to-end over fakes, and the SSE lab watermark."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import create_app
from swing_screener.cockpit.common import ActionNonce
from swing_screener.cockpit.routers.events import _change_token
from swing_screener.cockpit.routers.lab import _build_default_worker, _stalled
from swing_screener.db.models import LabAnalysis
from swing_screener.db.repo import create_lab_analysis, get_lab_analysis
from swing_screener.db.session import get_engine
from swing_screener.notify.analysis import LabDeepAnalysis, Usage
from swing_screener.settings import resolve_edge_dir

_HDR = {"X-Cockpit": "1"}


def _db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"


def _fake_bars(ticker: str, timeframe: str) -> pd.DataFrame:
    n = 80
    rng = np.random.default_rng(2)
    close = 50 + np.cumsum(rng.normal(0, 1, n))
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1,
         "close": close, "volume": [1e6] * n},
        index=pd.bdate_range("2026-01-01", periods=n),
    )


def _client(tmp_path: Path, *, bars=None, worker=None,
            raise_server_exceptions: bool = True) -> TestClient:
    url = _db_url(tmp_path)
    get_engine(url)  # create_all seeds the schema (incl. lab_analyses)
    return TestClient(
        create_app(url, edge_dir=tmp_path,
                   lab_bars=bars if bars is not None else _fake_bars,
                   lab_worker=worker if worker is not None else (lambda _id: None)),
        raise_server_exceptions=raise_server_exceptions,
    )


# ---------------- GET /api/lab/bars ----------------

def test_bars_payload_shape(tmp_path: Path) -> None:
    r = _client(tmp_path).get("/api/lab/bars",
                              params={"ticker": "nvda", "timeframe": "1d"})
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "NVDA" and body["timeframe"] == "1d"
    assert body["bar_count"] == 80 and len(body["candles"]) == 80
    assert set(body["emas"]) == {"9", "21", "50", "200"}
    assert set(body["macd"]) == {"macd", "signal", "hist"}
    assert set(body["levels"]) == {"support", "resistance"}
    assert body["emas"]["200"] == [None] * 80  # 80 bars: honest all-null, never biased


def test_bars_bad_timeframe_422(tmp_path: Path) -> None:
    r = _client(tmp_path).get("/api/lab/bars",
                              params={"ticker": "NVDA", "timeframe": "2h"})
    assert r.status_code == 422


def test_bars_blank_ticker_422(tmp_path: Path) -> None:
    r = _client(tmp_path).get("/api/lab/bars",
                              params={"ticker": "  ", "timeframe": "1d"})
    assert r.status_code == 422


def test_bars_upstream_failure_is_503_class_name_only(tmp_path: Path) -> None:
    def dead_bars(ticker: str, timeframe: str) -> pd.DataFrame:
        raise RuntimeError(
            "no hourly bars for NVDA: 502 from "
            "https://query1.finance.yahoo.com/v7?crumb=s3cret")

    r = _client(tmp_path, bars=dead_bars).get(
        "/api/lab/bars", params={"ticker": "NVDA", "timeframe": "4h"})
    assert r.status_code == 503
    assert r.json()["detail"] == "upstream error (RuntimeError)"
    assert "yahoo" not in r.text and "s3cret" not in r.text


def test_bars_genuine_bug_still_500s(tmp_path: Path) -> None:
    def buggy_bars(ticker: str, timeframe: str) -> pd.DataFrame:
        raise TypeError("boom")

    r = _client(tmp_path, bars=buggy_bars,
                raise_server_exceptions=False).get(
        "/api/lab/bars", params={"ticker": "NVDA", "timeframe": "1d"})
    assert r.status_code == 500


# ---------------- the deep-analysis queue ----------------

def test_analysis_requires_cockpit_header(tmp_path: Path) -> None:
    r = _client(tmp_path).post("/api/lab/analysis", json={"ticker": "NVDA"})
    assert r.status_code == 403


def test_analysis_queues_and_hands_off_to_the_worker(tmp_path: Path) -> None:
    drained: list[int] = []
    client = _client(tmp_path, worker=drained.append)
    r = client.post("/api/lab/analysis", json={"ticker": "nvda"}, headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "NVDA" and body["status"] == "queued"
    assert body["reasoning"] == "max"  # the lab default: full effort
    assert drained == [body["id"]]     # the in-process drain got the row id

    listed = client.get("/api/lab/analysis").json()["analyses"]
    assert [row["id"] for row in listed] == [body["id"]]
    assert listed[0]["report"] == "" and listed[0]["est_cost_usd"] is None


def test_analysis_list_filters_by_ticker(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/api/lab/analysis", json={"ticker": "AMD"}, headers=_HDR)
    client.post("/api/lab/analysis", json={"ticker": "NVDA"}, headers=_HDR)
    rows = client.get("/api/lab/analysis", params={"ticker": "amd"}).json()["analyses"]
    assert [r["ticker"] for r in rows] == ["AMD"]


def test_stalled_mirrors_the_drain_window() -> None:
    now = datetime.now(UTC)
    assert _stalled("running", now - timedelta(minutes=11), now=now) is True
    assert _stalled("running", now - timedelta(minutes=9), now=now) is False
    assert _stalled("queued", now - timedelta(hours=1), now=now) is False
    assert _stalled("running", None, now=now) is False


# ---------------- the default worker, end-to-end over fakes ----------------

def _seed_row(url: str, ticker: str = "NVDA") -> int:
    with Session(get_engine(url)) as s:
        row = create_lab_analysis(s, ticker=ticker,
                                  requested_at=datetime.now(UTC),
                                  model="claude-opus-4-8", reasoning="max")
        return row.id


def _patch_context(monkeypatch) -> None:
    monkeypatch.setattr(
        "swing_screener.notify.market_context.get_fundamentals", lambda t: None)
    monkeypatch.setattr(
        "swing_screener.notify.market_context.get_recent_news", lambda t: [])
    monkeypatch.setattr(
        "swing_screener.notify.market_context.context_block",
        lambda f, n: "ctx-block")


def test_default_worker_completes_the_row(tmp_path: Path, monkeypatch) -> None:
    url = _db_url(tmp_path)
    engine = get_engine(url)
    row_id = _seed_row(url)
    captured: dict[str, object] = {}

    def fake_analyze(ticker, facts, *, context_text="", client=None,
                     model="claude-opus-4-8", reasoning="max",
                     max_searches=4, web_search=True):
        captured.update(ticker=ticker, facts=facts, context=context_text,
                        model=model, reasoning=reasoning)
        return LabDeepAnalysis(
            report="## Read\nfine", is_deep=True,
            usage=Usage(input_tokens=10, output_tokens=20, web_searches=1,
                        est_cost_usd=0.42))

    _patch_context(monkeypatch)
    monkeypatch.setattr("swing_screener.notify.analysis.analyze_lab_deep",
                        fake_analyze)
    worker = _build_default_worker(lambda: engine, _fake_bars, ActionNonce())
    worker(row_id)

    with Session(engine) as s:
        row = get_lab_analysis(s, row_id)
        assert row is not None
        assert row.status == "done" and row.is_deep is True
        assert row.report == "## Read\nfine"
        assert row.est_cost_usd == 0.42
        assert row.started_at is not None and row.finished_at is not None
    # The model + effort frozen on the row at queue time are what actually ran.
    assert captured["model"] == "claude-opus-4-8"
    assert captured["reasoning"] == "max"
    assert "== 1d (daily)" in str(captured["facts"])
    assert captured["context"] == "ctx-block"


def test_default_worker_failure_stores_class_name_only(
        tmp_path: Path, monkeypatch) -> None:
    url = _db_url(tmp_path)
    engine = get_engine(url)
    row_id = _seed_row(url)

    def dead_bars(ticker: str, timeframe: str) -> pd.DataFrame:
        raise RuntimeError("secret host https://internal.example/creds")

    _patch_context(monkeypatch)
    worker = _build_default_worker(lambda: engine, dead_bars, ActionNonce())
    worker(row_id)

    with Session(engine) as s:
        row = get_lab_analysis(s, row_id)
        assert row is not None
        assert row.status == "failed"
        assert row.error == "error (RuntimeError)"
        assert "internal.example" not in (row.error or "")


def test_default_worker_skips_non_queued_rows(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    engine = get_engine(url)
    row_id = _seed_row(url)
    with Session(engine) as s:
        row = s.get(LabAnalysis, row_id)
        assert row is not None
        row.status = "done"
        s.commit()
    worker = _build_default_worker(
        lambda: engine,
        lambda t, tf: (_ for _ in ()).throw(AssertionError("must not fetch")),
        ActionNonce(),
    )
    worker(row_id)  # no-op: already terminal
    with Session(engine) as s:
        row = s.get(LabAnalysis, row_id)
        assert row is not None and row.status == "done"


# ---------------- the SSE watermark ----------------

def test_change_token_watches_the_lab(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    engine = get_engine(url)
    edge = resolve_edge_dir(tmp_path)
    before = _change_token(engine, edge)
    assert "lab" in before
    _seed_row(url)
    after = _change_token(engine, edge)
    assert after["lab"] != before["lab"]  # a new request moves the token

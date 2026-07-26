"""Events router (split from test_api.py): the /api/events SSE wake channel,
the change token's watermarks, and the post-action nonce."""

from collections.abc import MutableMapping
from datetime import date, datetime
import json
import os
from pathlib import Path
from typing import Any

import anyio
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import create_app
from swing_screener.cockpit.common import (
    ActionNonce,
)
from swing_screener.cockpit.routers import events as events_module
from swing_screener.cockpit.routers.events import _change_token, _safe_change_token
from swing_screener.cockpit.routers.reference import BOOKKEEPING_EMAIL_KINDS
from swing_screener.db.models import (
    AnalystCall,
    EmailLog,
    ExitEvent,
    Trade,
)
from swing_screener.db.repo import (
    claim_queued_requests,
    complete_analysis_request,
    create_analysis_request,
    requeue_stale_running,
)
from swing_screener.db.session import get_engine
from swing_screener.pipeline.proposed import (
    store_filename,
)
from swing_screener.pipeline.reflect import (
    verdicts_filename,
    verdicts_to_json,
)
from tests.cockpit.conftest import (
    _broker_app,
    _call,
    _client_and_engine,
    _db_url,
    _disarm_broker,
    _exec_log,
    _experiment,
    _HDR,
    _nonce_of,
    _proposal,
    _trade,
    _trade_body,
    _write_proposals,
    _write_registry,
)


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
    engine dies at query time, and the factory itself raises. The action nonce
    rides the SENTINEL too: proposal decisions are file-only writes that succeed
    with a dead DB, and their wake must not be swallowed by the down shape."""
    nonce = ActionNonce()
    bad = create_engine("sqlite:///Z:/definitely/nope/x.db")  # unopenable at query time
    assert _safe_change_token(lambda: bad, tmp_path, nonce) == {
        "db": "down", "action": "0"}

    def boom() -> Engine:
        raise RuntimeError("engine factory failed")

    assert _safe_change_token(boom, tmp_path, nonce) == {"db": "down", "action": "0"}
    nonce.bump()  # a bump still reads as a change while the DB is down
    assert _safe_change_token(boom, tmp_path, nonce) == {"db": "down", "action": "1"}


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


# ---- Task 12: the grown token (everything the six actions touch) ----


def test_change_token_watches_the_real_book(tmp_path: Path) -> None:
    """A REAL (manual) trade insert moves the token via its own watermark: the
    paper-book watermark (max(PaperTrade.id)) never covered the trades table, so
    without ``trade_real`` a logged trade would wait out the 60s poll floor."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    before = _change_token(engine, tmp_path)
    with Session(engine) as s:
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium",
                    entry_date=date.today(), entry_price=100.0, size=10.0,
                    stop=95.0, target=110.0))
        s.commit()
    after = _change_token(engine, tmp_path)
    assert after != before
    assert after["trade_real"] != before["trade_real"]  # the real book's own clock
    assert after["trade"] == before["trade"]  # the paper watermark stays blind to it


def test_close_trade_moves_the_token_via_the_exit_event(tmp_path: Path) -> None:
    """Wire-level: a manual close is an UPDATE on trades -- no new id, so
    ``trade_real`` (max(Trade.id)) is BLIND to it -- and the token still moves
    because the close endpoint inserts its ExitEvent in the same transaction."""
    client, engine = _client_and_engine(tmp_path)
    r = client.post("/api/trades", json=_trade_body(), headers=_HDR)
    assert r.status_code == 200
    before = _change_token(engine, tmp_path)
    rc = client.post(f"/api/trades/{r.json()['trade_id']}/close",
                     json={"exit_price": 106.0}, headers=_HDR)
    assert rc.status_code == 200
    after = _change_token(engine, tmp_path)
    assert after["exit"] != before["exit"]              # the ExitEvent insert
    assert after["trade_real"] == before["trade_real"]  # the UPDATE alone is invisible


def test_change_token_covers_the_whole_analysis_lifecycle(tmp_path: Path) -> None:
    """Every AnalysisRequest transition moves the token, including the two that are
    pure UPDATEs: claim (queued->running stamps started_at -- always the newest
    stamp) and REQUEUE (running->queued NULLS started_at). The requeue scenario is
    the adversarial one the plan flags: the requeued row's stamp is NOT the max
    (another running row holds a later one), so max(id)/max(finished_at)/
    max(started_at) ALL sit still -- only the running-count component moves."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    t1 = datetime(2026, 7, 10, 12, 0)
    t2 = datetime(2026, 7, 10, 13, 0)
    before = _change_token(engine, tmp_path)

    with Session(engine) as s:  # a new request: the id component
        create_analysis_request(s, ticker="AMD", requested_at=t1)
    after_create = _change_token(engine, tmp_path)
    assert after_create["analysis"] != before["analysis"]

    with Session(engine) as s:  # claim: an UPDATE -- started_at is the clock
        assert [r.ticker for r in claim_queued_requests(s, now=t1)] == ["AMD"]
    after_claim = _change_token(engine, tmp_path)
    assert after_claim["analysis"] != after_create["analysis"]

    with Session(engine) as s:  # a second request, claimed LATER -- holds the max
        create_analysis_request(s, ticker="NVDA", requested_at=t2)
        claimed = claim_queued_requests(s, now=t2)
        assert [r.ticker for r in claimed] == ["NVDA"]
        nvda_id = claimed[0].id
    after_second = _change_token(engine, tmp_path)

    with Session(engine) as s:  # requeue AMD only; NVDA keeps the later stamp
        assert requeue_stale_running(s, cutoff=datetime(2026, 7, 10, 12, 30)) == 1
    after_requeue = _change_token(engine, tmp_path)
    assert after_requeue != after_second
    second_parts = after_second["analysis"].split("|")
    requeue_parts = after_requeue["analysis"].split("|")
    assert requeue_parts[:3] == second_parts[:3]  # id/finished/started: ALL blind here
    assert requeue_parts[3] != second_parts[3]    # the count is the requeue clock

    with Session(engine) as s:  # completion: finished_at
        complete_analysis_request(s, nvda_id, summary="done", pdf_blob_key=None,
                                  chart_blob_keys="", finished_at=t2)
    after_done = _change_token(engine, tmp_path)
    assert after_done["analysis"] != after_requeue["analysis"]


def test_change_token_watches_the_execution_log(tmp_path: Path) -> None:
    url = _db_url(tmp_path)
    engine = get_engine(url)
    before = _change_token(engine, tmp_path)
    with Session(engine) as s:
        s.add(_exec_log())
        s.commit()
    after = _change_token(engine, tmp_path)
    assert after != before
    assert after["execution"] != before["execution"]


def test_change_token_email_watermark_ignores_bookkeeping_rows(tmp_path: Path) -> None:
    """The email watermark excludes exactly what the email SURFACES exclude.

    One alerted live rejection writes N per-row ``execution-cover`` coverage rows
    beside the ONE display row. Neither email surface renders coverage rows, so
    waking every open cockpit for one is a refetch with nothing to show. A display
    row still moves the mark."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    before = _change_token(engine, tmp_path)
    with Session(engine) as s:
        s.add(EmailLog(sent_at=datetime(2026, 7, 25, 12, 0),
                       kind=BOOKKEEPING_EMAIL_KINDS[0], subject="x",
                       run_date=date(2026, 7, 25), alert_key="xlog-1"))
        s.commit()
    assert _change_token(engine, tmp_path)["email"] == before["email"]
    with Session(engine) as s:  # the DISPLAY row for the same email does wake it
        s.add(EmailLog(sent_at=datetime(2026, 7, 25, 12, 1), kind="execution",
                       subject="live rejections", run_date=date(2026, 7, 25),
                       alert_key="abc"))
        s.commit()
    assert _change_token(engine, tmp_path)["email"] != before["email"]


def test_change_token_watches_analyst_calls_and_scoring(tmp_path: Path) -> None:
    """An AnalystCall insert moves max(id); SCORING is an UPDATE (scored_at set on
    the existing row) that max(id) can't see -- the scored-count component is its
    clock, mirroring the paper book's close-is-an-UPDATE lesson."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    before = _change_token(engine, tmp_path)
    with Session(engine) as s:
        call = _call(date.today(), 0.5)
        s.add(call)
        s.commit()
        call_id = call.id
    after_insert = _change_token(engine, tmp_path)
    assert after_insert["analyst"] != before["analyst"]
    with Session(engine) as s:
        row = s.get(AnalystCall, call_id)
        assert row is not None
        row.realized_r = 1.2
        row.scored_at = date.today()
        s.commit()
    after_score = _change_token(engine, tmp_path)
    assert after_score != after_insert
    parts_before = after_insert["analyst"].split("|")
    parts_after = after_score["analyst"].split("|")
    assert parts_after[0] == parts_before[0]  # max(id): blind to the UPDATE
    assert parts_after[1] != parts_before[1]  # the scored count moved


def test_change_token_watches_proposal_stores_and_registry(tmp_path: Path) -> None:
    """The edge-dir decision files ride the token as mtimes: a proposal store
    appearing, a store rewritten in place (what a decision does -- pinned via
    os.utime so filesystem mtime granularity can't flake the test), and the
    experiment registry appearing each move their own key."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    before = _change_token(engine, tmp_path)
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    after_store = _change_token(engine, tmp_path)
    assert after_store["proposals"] != before["proposals"]
    path = tmp_path / store_filename("reversal")
    st = path.stat()
    # +1s, not +1ns: NTFS keeps mtimes at 100ns resolution and other filesystems
    # are coarser still -- a sub-quantum bump would round away and flake.
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    after_rewrite = _change_token(engine, tmp_path)
    assert after_rewrite["proposals"] != after_store["proposals"]
    assert after_rewrite["registry"] == after_store["registry"]  # still absent
    _write_registry(tmp_path, [_experiment("exp1", kind="arm")])
    assert _change_token(engine, tmp_path)["registry"] != after_rewrite["registry"]


def test_change_token_watches_verdicts_sidecar_rewrites(tmp_path: Path) -> None:
    """The verdicts key rides ``_file_watermark`` (st_mtime_ns) over the two
    play-type sidecars -- NOT the heartbeat's float-mtime ``newest_verdicts_mtime``
    (which genuinely wants a datetime): reflect's ``--verdicts-only`` mode REWRITES
    ``edge/<pt>.verdicts.json`` IN PLACE, and only the integer-ns clock promises
    same-second rewrite detection. A sidecar appearing and an in-place rewrite
    (mtime bumped +1s via os.utime -- the NTFS-honest technique) each move the
    token; the path comes from reflect's own ``verdicts_filename``, so the test
    breaks if the token ever watches a differently-named file than reflect writes."""
    url = _db_url(tmp_path)
    engine = get_engine(url)
    before = _change_token(engine, tmp_path)
    sidecar = tmp_path / verdicts_filename("reversal")
    sidecar.write_text(verdicts_to_json([]), encoding="utf-8")
    after_write = _change_token(engine, tmp_path)
    assert after_write["verdicts"] != before["verdicts"]
    st = sidecar.stat()
    os.utime(sidecar, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert _change_token(engine, tmp_path)["verdicts"] != after_write["verdicts"]


# ---- Task 12: the post-action nonce ----


def test_action_nonce_bumps_once_per_successful_action_post(tmp_path: Path) -> None:
    """Each action POST bumps the app's nonce EXACTLY once on success and never on
    a 4xx: log trade (403 headerless / 422 invalid stay flat), close trade (409
    re-close stays flat), request analysis (422 stays flat), approve (409 double-
    approve stays flat) and withdraw (404 unknown stays flat). Exact values per
    step, so removing any single endpoint's bump fails at that step."""
    url = _db_url(tmp_path)
    get_engine(url)
    _write_proposals(tmp_path, "reversal", [
        _proposal("r1", "reversal", {"max_extension_atr": 1.5}),
        _proposal("r2", "reversal", {"min_target_r": 2.0}),
    ])
    app = create_app(url, edge_dir=tmp_path)
    client = TestClient(app)
    nonce = _nonce_of(app)
    assert nonce.value == 0

    r = client.post("/api/trades", json=_trade_body(), headers=_HDR)
    assert r.status_code == 200 and nonce.value == 1
    assert client.post("/api/trades", json=_trade_body()).status_code == 403
    assert nonce.value == 1  # headerless: guard fired, nothing written, no wake
    bad = _trade_body(stop=105.0)
    assert client.post("/api/trades", json=bad, headers=_HDR).status_code == 422
    assert nonce.value == 1  # rejected body: no wake

    trade_id = r.json()["trade_id"]
    rc = client.post(f"/api/trades/{trade_id}/close",
                     json={"exit_price": 106.0}, headers=_HDR)
    assert rc.status_code == 200 and nonce.value == 2
    rc = client.post(f"/api/trades/{trade_id}/close",
                     json={"exit_price": 106.0}, headers=_HDR)
    assert rc.status_code == 409 and nonce.value == 2  # re-close refused: no wake

    r = client.post("/api/analysis", json={"ticker": "AMD"}, headers=_HDR)
    assert r.status_code == 200 and nonce.value == 3
    r = client.post("/api/analysis", json={"ticker": ""}, headers=_HDR)
    assert r.status_code == 422 and nonce.value == 3

    r = client.post("/api/proposals/reversal/r1/approve",
                    json={"reason": "worth a slot"}, headers=_HDR)
    assert r.status_code == 200 and nonce.value == 4
    r = client.post("/api/proposals/reversal/r1/approve",
                    json={"reason": "again"}, headers=_HDR)
    assert r.status_code == 409 and nonce.value == 4  # refused transition: no wake

    r = client.post("/api/proposals/reversal/r2/withdraw",
                    json={"reason": "not now"}, headers=_HDR)
    assert r.status_code == 200 and nonce.value == 5
    r = client.post("/api/proposals/reversal/nope/withdraw",
                    json={"reason": "ghost"}, headers=_HDR)
    assert r.status_code == 404 and nonce.value == 5


def test_disarm_bumps_the_nonce_only_on_a_real_run(tmp_path: Path) -> None:
    """DISARM bumps on a REAL run only: a dry-run preview changes nothing at the
    venue (and Task 15's hold-to-confirm fires one on every hold-start -- waking
    all windows per hold would be noise), and the no-broker 409 never bumps
    (nothing touched the venue). A real run that FAILS partway still bumps --
    pinned by test_disarm_broker_failure_is_503_class_only_and_busts_the_snapshot."""
    broker = _disarm_broker()
    client, _engine, _calls = _broker_app(tmp_path, broker)
    nonce = _nonce_of(client.app)
    r = client.post("/api/disarm?dry_run=1", headers=_HDR)
    assert r.status_code == 200 and nonce.value == 0  # preview: no wake
    r = client.post("/api/disarm", headers=_HDR)
    assert r.status_code == 200 and nonce.value == 1  # the venue moved: wake

    no_broker_client, _e, _c = _broker_app(tmp_path, None)
    no_broker_nonce = _nonce_of(no_broker_client.app)
    assert no_broker_client.post("/api/disarm", headers=_HDR).status_code == 409
    assert no_broker_nonce.value == 0


def _sse_data(chunk: bytes) -> dict[str, str]:
    """The JSON payload of one SSE 'change' event chunk."""
    line = next(ln for ln in chunk.decode().splitlines() if ln.startswith("data: "))
    data = json.loads(line[len("data: "):])
    assert isinstance(data, dict)
    return data


def test_events_stream_wakes_on_a_nonce_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonce bump reaches the WIRE with no DB write at all: the stream's next
    token poll sees the bumped counter and emits a second change event whose only
    moved key is ``action``. ``_WAKE_POLL_S`` is shrunk so 'next poll tick' is
    milliseconds -- the cadence itself is Phase 2's shipped contract, not under
    test. Same raw-ASGI drive as test_events_route_exists (TestClient buffers
    infinite streams)."""
    monkeypatch.setattr(events_module, "_WAKE_POLL_S", 0.01)
    url = _db_url(tmp_path)
    get_engine(url)
    app = create_app(url, edge_dir=tmp_path)
    nonce = _nonce_of(app)

    async def _two_events() -> list[dict[str, str]]:
        tokens: list[dict[str, str]] = []
        done = anyio.Event()

        async def receive() -> dict[str, Any]:
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            if message["type"] != "http.response.body":
                return
            body = bytes(message.get("body") or b"")
            if b"event: change" not in body:
                return  # keepalive pings ride the same cadence; ignore them
            tokens.append(_sse_data(body))
            if len(tokens) == 1:
                nonce.bump()  # what a successful action POST does, minus the POST
            else:
                done.set()

        scope: dict[str, Any] = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "path": "/api/events", "raw_path": b"/api/events", "root_path": "",
            "scheme": "http", "query_string": b"", "headers": [],
            "client": ("testclient", 50000), "server": ("testserver", 80),
        }
        with anyio.fail_after(10):  # a wedged stream FAILS the test, never hangs it
            await app(scope, receive, send)
        return tokens

    first, second = anyio.run(_two_events)[:2]
    assert first["action"] == "0"
    assert second["action"] == "1"
    # the nonce is the ONLY mover: no DB write happened between the two events
    assert {k: v for k, v in first.items() if k != "action"} == \
           {k: v for k, v in second.items() if k != "action"}



"""Trades-router write actions (split from test_api.py): POST /api/trades,
POST /api/trades/{id}/close, and the _override_note branch pins."""

from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from swing_screener.cockpit.routers.trades import (
    TradeCreate,
    _override_note,
)
from swing_screener.db.models import (
    ExitEvent,
    Trade,
)
from tests.cockpit.conftest import (
    _client_and_engine,
    _HDR,
    _signal_row,
    _trade_body,
)


def test_manual_close_writes_review_facts_and_enqueues_draft(tmp_path: Path) -> None:
    """On close: the deterministic Coach review facts row is written synchronously and
    an async draft is enqueued -- no LLM on the HTTP path (the worker drains it)."""
    import json as _json

    from swing_screener.db.models import CoachDraftRequest, JournalReview

    client, engine = _client_and_engine(tmp_path)
    r = client.post("/api/trades", json=_trade_body(entry_price=100.0, stop=95.0,
                                                     target=110.0), headers=_HDR)
    tid = r.json()["trade_id"]
    rc = client.post(f"/api/trades/{tid}/close",
                     json={"exit_price": 110.0, "exit_reason": "target"}, headers=_HDR)
    assert rc.status_code == 200
    with Session(engine) as s:
        review = s.query(JournalReview).filter_by(trade_id=tid, kind="trade_close").one()
        assert review.book == "manual_equity"
        assert review.source == "analyst"
        assert review.narrative is None                 # drafted later, async
        facts = _json.loads(review.facts_json)
        assert facts["result"] == 2.0 and facts["outcome"] == "target"  # (110-100)/(100-95)
        assert "tag_proposals" in facts                 # parked, not applied
        draft = s.query(CoachDraftRequest).filter_by(review_id=review.id).one()
        assert draft.status == "queued"


# ---- trade write actions: POST /api/trades + POST /api/trades/{id}/close ----


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


def test_log_trade_captures_emotional_state(tmp_path: Path) -> None:
    # Journal v2: the discretionary entry emotion rides on the manual trade (Coach data).
    client, engine = _client_and_engine(tmp_path)
    r = client.post("/api/trades", json=_trade_body(emotional_state="fomo"), headers=_HDR)
    assert r.status_code == 200
    with Session(engine) as s:
        t = s.get(Trade, r.json()["trade_id"])
        assert t is not None and t.emotional_state == "fomo"


def test_log_trade_emotional_state_defaults_none(tmp_path: Path) -> None:
    client, engine = _client_and_engine(tmp_path)
    r = client.post("/api/trades", json=_trade_body(), headers=_HDR)
    with Session(engine) as s:
        assert s.get(Trade, r.json()["trade_id"]).emotional_state is None


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
    # commit 1 = the atomic close+ExitEvent (proven below by count==2, not orphaned);
    # commit 2 = the Coach close-review follow-on, which fails here and is caught
    # (best-effort, never 503s the close). The close+event are still ONE transaction.
    assert len(commits) == 2
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



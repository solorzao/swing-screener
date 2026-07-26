"""Guardrail-trip + live-rejection alert emails (Task 10, amended in Task 11).

Two standalone alert kinds, both send-then-log with ``EmailLog`` rows (the
``_emit_pending_exit_alert`` ordering: a failed send leaves NO row, so the retry
owner re-sends; the pre-check makes a sequential re-run a no-op). The contracts
live in ``notify.alerts`` (the pipeline jobs share them), so these tests exercise
THAT module's public functions; ``notify.run``'s one-line delegates get one
still-wired test per path rather than being the subject of the contract tests.

The load-bearing properties proven here:

* IMMEDIACY: ``respond_to_trip`` with the real ``_trip_emailer`` sends exactly
  ONE alert and logs ``EmailLog(kind='guardrail', alert_key=str(trip_event_id))``;
  the at-least-once retry emitter then no-ops on the same key.
* AT-LEAST-ONCE: a raising send leaves NO log row, and
  ``alerts.emit_pending_guardrail_alert`` (the retry owner -- elections happen
  once per trip, so the in-protocol path never retries) sends on a later cycle.
* The broker-None evening-trip case (tripped state, no mail ever sent) is
  covered by a full ``send_digest`` run.
* A dead mailer never aborts the sweep (the trip protocol's swallow posture).
* REJECTIONS: only ``rejected_live`` flips are mailed (Task-11 review --
  ``canceled`` covers benign EOD DAY expiry AND our own trip/halt/kill sweep
  cancels, so alerting it would tell the operator to re-enter orders the
  guardrails deliberately killed). One email writes ONE ``kind='execution'``
  DISPLAY row (what the cockpit lists) plus one ``kind='execution-cover'``
  coverage row per alerted ExecutionLog id (``alert_key='xlog-{id}'`` -- Task 11
  replaced the sha1-of-the-set coverage key, whose partial overlaps re-alerted
  covered rows); re-runs flip nothing new and the emitter dedups per row. A
  venue stop-out is a CLOSE (fill + broker_close -> the EXIT alert), never a
  rejection -- the two kinds are disjoint by construction.

All tmp-file sqlite + FakeBroker + injected send spies -- no venue, no SMTP.
"""

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db import guardrails_repo as gr
from swing_screener.db import repo
from swing_screener.db.models import EmailLog, ExecutionLog
from swing_screener.db.session import get_engine
from swing_screener.notify import alerts, run
from swing_screener.pipeline import guardrails as gp
from swing_screener.pipeline.broker import BrokerOrderSpec, FakeBroker
from swing_screener.pipeline.execution import LiveAdapter
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.settings import Limits

RUN = date(2026, 6, 15)
BREAKER = "max_daily_loss_usd"
REASON = "max daily loss: $-60.00 <= -$50.00"
NO_LIMITS = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=None)


def _spy():
    sent: list[dict] = []
    return sent, (lambda **kw: sent.append(kw))


def _dead_send(**kw):
    raise RuntimeError("smtp down")


def _entry_spec(key: str, symbol: str) -> BrokerOrderSpec:
    return BrokerOrderSpec(client_order_id=key, symbol=symbol, side="buy", qty=10,
                           order_type="limit", limit_price=100.0, time_in_force="day")


def _submit_live(url: str, broker: FakeBroker, ticker: str) -> str:
    """One REAL submitted_live ExecutionLog via the LiveAdapter (the reconcile's input)."""
    intent = OrderIntent(
        ticker=ticker, timeframe="1d", play_type="continuation",
        entry_floor=99.0, entry_ceiling=101.0, stop=94.0, target=110.0,
        conviction="high", shares=10, risk_dollars=70.0,
        edge_played="e", key_risk="", insight="i", side="long", limit_price=101.0)
    with Session(get_engine(url)) as s:
        adapter = LiveAdapter(broker, gate_ready_fn=lambda _s: True)
        result = adapter.submit(intent, session=s, run_date=date(2026, 6, 14),
                                limits=NO_LIMITS)
        s.commit()
    assert result.broker_order_id is not None
    return result.broker_order_id


def _digest_kwargs(tmp_path, url, sent, **extra):
    return dict(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                pdf_dir=tmp_path / "digests", smtp_send=lambda **k: sent.append(k),
                **extra)


# --- guardrail-trip alerts ---------------------------------------------------


def test_trip_sends_exactly_one_email(tmp_path):
    """respond_to_trip with the real emailer: ONE send, EmailLog kind='guardrail'
    keyed str(trip_event_id) -- and the at-least-once retry emitter then no-ops
    on the same key (immediacy + at-least-once collapse onto one dedup row)."""
    url = f"sqlite:///{tmp_path / 'trip.sqlite'}"
    sent, send = _spy()
    with Session(get_engine(url)) as s:
        emailer = run._trip_emailer(s, run_date=RUN, recipient="me@example.com", send=send)
        trip_id = gp.respond_to_trip(s, breaker=BREAKER, reason=REASON,
                                     source="digest", broker=FakeBroker(), emailer=emailer)
        assert trip_id is not None
        assert len(sent) == 1
        assert sent[0]["to"] == "me@example.com"
        assert "GUARDRAIL TRIPPED" in sent[0]["subject"]
        assert BREAKER in sent[0]["subject"]
        assert REASON in sent[0]["text"]          # the pre-formatted reason, verbatim
        # phone-glance: the REASON (dollar figures) leads the body, so a
        # lock-screen preview (subject + first line) already answers "why".
        assert REASON in sent[0]["text"].splitlines()[0]
        row = s.scalars(select(EmailLog).where(EmailLog.kind == "guardrail")).one()
        assert row.alert_key == str(trip_id)
        assert row.run_date == RUN
        # the shared retry emitter sees the log row -> no second send.
        assert alerts.emit_pending_guardrail_alert(
            s, RUN, "me@example.com", send) is False
        assert len(sent) == 1


def test_trip_email_failure_leaves_no_log_row(tmp_path):
    """SEND-then-LOG: a dead transport leaves NO EmailLog row, so the retry
    emitter sends on the next cycle (a later run_date -- the dedup is keyed on
    the trip id, NOT the date) and succeeds.

    Goes through ``run._emit_pending_guardrail_alert`` on purpose: the DELEGATE
    test for this path (the contract itself is exercised against
    ``alerts.emit_pending_guardrail_alert`` above)."""
    url = f"sqlite:///{tmp_path / 'tripfail.sqlite'}"
    sent, send = _spy()
    with Session(get_engine(url)) as s:
        emailer = run._trip_emailer(s, run_date=RUN, recipient="me@example.com",
                                    send=_dead_send)
        trip_id = gp.respond_to_trip(s, breaker=BREAKER, reason=REASON,
                                     source="digest", broker=FakeBroker(), emailer=emailer)
        assert trip_id is not None                # the protocol survived the dead mailer
        assert s.query(EmailLog).count() == 0     # no row -> the retry stays armed
        # the NEXT cycle's digest owns the retry.
        assert run._emit_pending_guardrail_alert(
            s, RUN + timedelta(days=1), "me@example.com", send) is True
        assert len(sent) == 1
        assert "GUARDRAIL TRIPPED" in sent[0]["subject"]
        row = s.scalars(select(EmailLog).where(EmailLog.kind == "guardrail")).one()
        assert row.alert_key == str(trip_id)


def test_digest_side_emitter_covers_unmailed_trip(tmp_path, monkeypatch):
    """The broker-None evening-trip case: the screen tripped (sweep deferred, no
    mail ever sent); the next digest RUN itself sends the alert from its alert
    section -- the digest is the retry owner, exactly like exit alerts."""
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("SWING_DEEP_ANALYSIS", raising=False)
    url = f"sqlite:///{tmp_path / 'digestretry.sqlite'}"
    with Session(get_engine(url)) as s:
        trip_id = gr.trip(s, breaker=BREAKER, reason=REASON, source="screen")
        assert trip_id is not None
        assert gr.load_guardrails(s).sweep_state == "pending"
    sent: list[dict] = []

    res = run.send_digest(**_digest_kwargs(tmp_path, url, sent))

    assert res.sent is True
    subjects = [m["subject"] for m in sent]
    assert any("GUARDRAIL TRIPPED" in x for x in subjects)
    assert any("Daily Picks" in x for x in subjects)
    with Session(get_engine(url)) as s:
        row = s.scalars(select(EmailLog).where(EmailLog.kind == "guardrail")).one()
        assert row.alert_key == str(trip_id)

    # a re-run dedups on the EmailLog row: no second guardrail alert.
    sent2: list[dict] = []
    run.send_digest(**_digest_kwargs(tmp_path, url, sent2))
    assert [m for m in sent2 if "GUARDRAIL TRIPPED" in m["subject"]] == []


def test_trip_email_failure_never_aborts_sweep(tmp_path):
    """The mail seam is step 4 of the ordered protocol: a raising send must leave
    the trip persisted and the sweep outcome recorded ('complete' -- the broker
    was in scope and its resting entry was pulled)."""
    url = f"sqlite:///{tmp_path / 'sweep.sqlite'}"
    broker = FakeBroker()
    broker.submit_order(_entry_spec("rest-1", "TSLA"))    # a resting entry to pull
    with Session(get_engine(url)) as s:
        emailer = run._trip_emailer(s, run_date=RUN, recipient="me@example.com",
                                    send=_dead_send)
        trip_id = gp.respond_to_trip(s, breaker=BREAKER, reason=REASON,
                                     source="digest", broker=broker, emailer=emailer)
        assert trip_id is not None
        g = gr.load_guardrails(s)
        assert g.state == "tripped"
        assert g.sweep_state == "complete"        # outcome recorded BEFORE the mail attempt
    assert broker.list_open_orders() == []        # the sweep really ran


# --- live-rejection alerts ---------------------------------------------------


def test_rejection_alert_lists_rejected_rows_only(tmp_path, monkeypatch):
    """Two working live orders die at the venue -- one REJECTED, one CANCELED.

    The dispatch-time reconcile flips both logs, but only the rejection is
    mailed (Task-11 review: a canceled row is benign DAY expiry or one of our
    own sweep cancels -- "re-enter manually if still wanted" would be advice to
    undo the guardrails). The one email writes ONE display row plus one coverage
    row for the alerted id -- and none for the canceled one, so no later pass
    thinks it is owed an alert."""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "live")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS", raising=False)
    url = f"sqlite:///{tmp_path / 'reject.sqlite'}"
    broker = FakeBroker()
    oid_a = _submit_live(url, broker, "AMD")
    oid_b = _submit_live(url, broker, "NVDA")
    broker.reject(oid_a)                          # venue reject  -> rejected_live
    broker.cancel_order(oid_b)                    # venue cancel  -> canceled
    sent: list[dict] = []

    res = run.send_digest(**_digest_kwargs(tmp_path, url, sent, broker=broker))

    assert res.sent is True
    rejections = [m for m in sent if "Rejected" in m["subject"]]
    assert len(rejections) == 1
    # phone-glance subject: count + tickers ride the preview.
    assert "1 Live Order Rejected" in rejections[0]["subject"]
    assert "AMD" in rejections[0]["subject"]
    body = rejections[0]["text"]
    assert "AMD" in body and "rejected_live" in body
    assert "NVDA" not in body                     # the canceled order is NOT alerted
    assert "re-enter manually" in body            # the action line
    with Session(get_engine(url)) as s:
        statuses = {(x.ticker, x.status) for x in s.scalars(select(ExecutionLog))}
        assert statuses == {("AMD", "rejected_live"), ("NVDA", "canceled")}
        amd_id = s.scalars(select(ExecutionLog.id)
                           .where(ExecutionLog.status == "rejected_live")).one()
        # ONE display row per EMAIL (what the cockpit's email list renders)...
        display = s.scalars(select(EmailLog).where(EmailLog.kind == "execution")).one()
        assert display.subject == rejections[0]["subject"]
        assert display.run_date == RUN
        # ...and per-ROW coverage rows (Task 11) for the alerted ids ONLY -- the
        # hourly retry's coverage join reads these, so a partial overlap alerts
        # only the uncovered rows.
        cover = list(s.scalars(select(EmailLog).where(EmailLog.kind == "execution-cover")))
        assert {r.alert_key for r in cover} == {f"xlog-{amd_id}"}
        assert all(r.run_date == RUN for r in cover)


def test_rejection_alert_dedups_on_rerun(tmp_path, monkeypatch):
    """A forced digest re-run re-polls the broker but flips nothing new (the
    reconcile's status-transition guard) -> no second rejection email; and the
    emitter itself no-ops on the covered rows even when handed the SAME id set.

    The final block goes through ``run._emit_live_rejection_alert``: the
    DELEGATE test for this path (the contract is exercised against
    ``alerts.send_live_rejection_alert`` below)."""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "live")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS", raising=False)
    url = f"sqlite:///{tmp_path / 'rerun.sqlite'}"
    broker = FakeBroker()
    broker.reject(_submit_live(url, broker, "AMD"))
    sent: list[dict] = []
    run.send_digest(**_digest_kwargs(tmp_path, url, sent, broker=broker))
    assert len([m for m in sent if "Rejected" in m["subject"]]) == 1

    sent2: list[dict] = []
    run.send_digest(**_digest_kwargs(tmp_path, url, sent2, broker=broker, force=True))
    assert [m for m in sent2 if "Rejected" in m["subject"]] == []

    with Session(get_engine(url)) as s:
        ids = set(s.scalars(select(ExecutionLog.id)))
        sent3, send3 = _spy()
        assert run._emit_live_rejection_alert(s, RUN, "me@example.com", send3, ids) is False
        assert sent3 == []
        assert s.query(EmailLog).filter_by(kind="execution").count() == 1
        assert s.query(EmailLog).filter_by(kind="execution-cover").count() == 1


def test_rejection_send_failure_leaves_no_log_row_then_retry_succeeds(tmp_path):
    """The shared emitter honors SEND-then-LOG too: a dead transport leaves NO
    rows at all (neither the display row nor any coverage row), so a later
    caller with the SAME id set (the Task-11 hourly job reuses this emitter)
    re-sends and logs."""
    url = f"sqlite:///{tmp_path / 'rejectfail.sqlite'}"
    with Session(get_engine(url)) as s:
        row = repo.add_execution_log(
            s, created_date=RUN, ticker="AMD", timeframe="1d",
            play_type="continuation", run_date=RUN, account="live", mode="live",
            side="buy", limit_price=100.0, shares=10, stop=95.0, target=110.0,
            risk_dollars=50.0, notional=1000.0, status="rejected_live",
            detail="insufficient buying power", idempotency_key="k-amd")
        new_ids = {row.id}
        try:
            alerts.send_live_rejection_alert(
                s, run_date=RUN, recipient="me@example.com", send=_dead_send,
                candidate_ids=new_ids)
        except RuntimeError:
            pass                                  # send_digest's reconcile guard swallows this
        assert s.query(EmailLog).count() == 0     # no row -> retry stays armed
        sent, send = _spy()
        assert alerts.send_live_rejection_alert(
            s, run_date=RUN, recipient="me@example.com", send=send,
            candidate_ids=new_ids) is True
        assert len(sent) == 1
        assert "AMD" in sent[0]["text"]
        assert "insufficient buying power" in sent[0]["text"]
        assert s.query(EmailLog).filter_by(kind="execution").count() == 1
        assert s.query(EmailLog).filter_by(kind="execution-cover").count() == 1


def test_canceled_rows_are_never_alertable(tmp_path):
    """The scope rule at the QUERY level (Task-11 review): a canceled row is
    never a rejection-alert candidate, and even handed one explicitly the
    emitter refuses to compose an email about it -- so the system's own
    trip/halt/kill sweep cancels can never generate "re-enter manually" advice.
    """
    url = f"sqlite:///{tmp_path / 'canceled.sqlite'}"
    with Session(get_engine(url)) as s:
        row = repo.add_execution_log(
            s, created_date=RUN, ticker="NVDA", timeframe="1d",
            play_type="continuation", run_date=RUN, account="live", mode="live",
            side="buy", limit_price=100.0, shares=10, stop=95.0, target=110.0,
            risk_dollars=50.0, notional=1000.0, status="canceled",
            detail="canceled by the guardrail sweep", idempotency_key="k-nvda")

        assert alerts.recent_rejection_ids(s, run_date=RUN) == set()
        assert alerts.pending_rejection_ids(s, run_date=RUN) == set()
        sent, send = _spy()
        assert alerts.send_live_rejection_alert(
            s, run_date=RUN, recipient="me@example.com", send=send,
            candidate_ids={row.id}) is False
        assert sent == []
        assert s.query(EmailLog).count() == 0


def test_malformed_coverage_key_is_skipped_not_fatal(tmp_path, monkeypatch, caplog):
    """The coverage-key parse is GUARDED: an unparseable ``execution-cover`` key is
    dropped with a warning instead of raising, so one bad row can never take the
    whole at-least-once retry pass down with it. Being wrong here re-alerts a row
    (an extra email), never a silent hole.

    Unreachable through today's key format -- the ``IN`` filter only ever selects
    keys ``_rejection_key`` generated -- so the test forces the shape a future key
    format, or a hand-written row, would produce."""
    url = f"sqlite:///{tmp_path / 'badkey.sqlite'}"
    with Session(get_engine(url)) as s:
        row = repo.add_execution_log(
            s, created_date=RUN, ticker="NVDA", timeframe="1d",
            play_type="continuation", run_date=RUN, account="live", mode="live",
            side="buy", limit_price=100.0, shares=10, stop=95.0, target=110.0,
            risk_dollars=50.0, notional=1000.0, status="rejected_live",
            detail="insufficient buying power", idempotency_key="k-nvda")
        monkeypatch.setattr(alerts, "_rejection_key", lambda i: f"xlog-{i}x")
        s.add(EmailLog(sent_at=datetime(2026, 6, 15, 12, 0),
                       kind=alerts.REJECTION_COVER_KIND, subject="covered",
                       run_date=RUN, alert_key=f"xlog-{row.id}x"))
        s.commit()

        with caplog.at_level(logging.WARNING, logger="swing_screener.notify.alerts"):
            # the coverage row exists but cannot be parsed -> the row reads UNCOVERED
            assert alerts.pending_rejection_ids(s, run_date=RUN) == {row.id}
        assert "malformed" in caplog.text

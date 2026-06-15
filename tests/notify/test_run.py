from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import EmailLog, ExitEvent, Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import run

RUN = date(2026, 6, 15)


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeClient:
    @property
    def messages(self):
        class _M:
            def create(self, **kw):
                return _Resp("CORE: Strong daily continuation.\n\nClean setup, entry to target.")

        return _M()


def _sig(ticker, rank):
    return Signal(run_date=RUN, ticker=ticker, timeframe="1d", horizon="medium",
                  score=1.0 / rank, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
                  entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0)


def _seed(url):
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_sig("AMD", 1), _sig("AEP", 2)])
        s.commit()


def test_send_digest_emails_with_pdf_and_is_idempotent(tmp_path):
    url = f"sqlite:///{tmp_path / 'd.sqlite'}"
    _seed(url)
    sent = []

    def recorder(**kwargs):
        sent.append(kwargs)

    res = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                          smtp_send=recorder)
    assert res.sent is True and res.n_picks == 2 and res.pdf_attached is True
    assert len(sent) == 1
    email = sent[0]
    assert email["to"] == "me@example.com"
    assert "AMD" in email["text"]
    assert len(email["attachments"]) == 1  # the PDF
    assert str(email["attachments"][0]).endswith(".pdf")

    # second run for the same day is a no-op (idempotent)
    res2 = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                           pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                           smtp_send=recorder)
    assert res2.sent is False
    assert len(sent) == 1  # recorder not called again


def test_email_log_sent_at_is_stamped_in_utc(tmp_path):
    # sent_at must be the UTC wall-clock, not the host's local time, so container
    # timestamps are unambiguous. The stored value is naive but represents UTC;
    # comparing against now-in-UTC catches a regression to naive local
    # datetime.now() on any host whose local tz != UTC.
    url = f"sqlite:///{tmp_path / 'utc.sqlite'}"
    _seed(url)
    run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                    pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                    smtp_send=lambda **kw: None)

    with Session(get_engine(url)) as s:
        row = s.scalars(select(EmailLog).where(EmailLog.kind == "daily")).first()
    assert row is not None
    stored = row.sent_at.replace(tzinfo=None) if row.sent_at.tzinfo else row.sent_at
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    assert abs((now_utc - stored).total_seconds()) < 300


def test_exit_alert_delivered_on_rerun_after_event(tmp_path):
    # an exit event that fires AFTER the digest already went out must still be
    # delivered on a re-run, even though the digest itself is a no-op.
    url = f"sqlite:///{tmp_path / 'e.sqlite'}"
    _seed(url)
    sent = []

    def recorder(**kwargs):
        sent.append(kwargs)

    kw = dict(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
              pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(), smtp_send=recorder)
    run.send_digest(**kw)  # first run: digest only (no exit events yet)
    assert len(sent) == 1

    with Session(get_engine(url)) as s:  # a hard stop fires intraday
        s.add(ExitEvent(created_date=RUN, is_paper=False, trade_id=1, tier="hard",
                        reason="stop", message="AMD stopped @ 95"))
        s.commit()

    res = run.send_digest(**kw)  # re-run: digest no-op, but exit alert still sent
    assert res.sent is False
    assert len(sent) == 2
    assert "Exit" in sent[1]["subject"] and sent[1]["attachments"] == []


def _real_open_trade(ticker):
    from swing_screener.db.models import Trade
    return Trade(ticker=ticker, timeframe="1d", horizon="medium", entry_date=date(2026, 6, 10),
                 entry_price=100.0, size=10.0, stop=95.0, target=110.0, status="open")


def test_kind_exit_produces_and_sends_exit_alert(tmp_path):
    # the exit kind PRODUCES today's real exit events (via run_exit_check) and
    # then SENDS the exit-alert email -- end to end, no network.
    url = f"sqlite:///{tmp_path / 'k.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(_real_open_trade("AMD"))  # a real, open trade that will hard-stop
        s.commit()

    sent = []

    def recorder(**kwargs):
        sent.append(kwargs)

    # fake live bar source: AMD's low breaches its stop -> hard exit.
    def fake_bars(tickers, timeframe):  # noqa: ARG001
        bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False, "bearish": True}
        return {t: bar for t in tickers}

    result = run.run_exit_check_and_alert(db_url=url, run_date=RUN, to="me@example.com",
                                          smtp_send=recorder, latest_bars_fn=fake_bars)

    assert result.n_open == 1 and result.n_exited == 1
    assert len(sent) == 1
    email = sent[0]
    assert email["to"] == "me@example.com"
    assert "Exit" in email["subject"]
    assert email["attachments"] == []  # exit alerts carry no PDF
    assert "AMD" in email["text"]

    # the produced event is a real (is_paper=False) exit event.
    with Session(engine) as s:
        events = list(s.scalars(select(ExitEvent).where(ExitEvent.is_paper.is_(False))))
        assert len(events) == 1 and events[0].reason == "stop"


def _exit_event(ticker, tier="hard", reason="stop"):
    return ExitEvent(created_date=RUN, is_paper=False, trade_id=1, tier=tier, reason=reason,
                     message=f"{ticker} stopped @ 95")


def test_exit_alert_key_distinguishes_event_sets(tmp_path):
    # the key is a deterministic hash over the SET of event ids, so {1,2},
    # {1,2,3}, and {1,3} all map to distinct keys (and order doesn't matter).
    url = f"sqlite:///{tmp_path / 'key.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_exit_event("AMD"), _exit_event("AEP"), _exit_event("MSFT")])
        s.commit()
        evs = list(s.scalars(select(ExitEvent).order_by(ExitEvent.id)))
    e1, e2, e3 = evs

    assert run._exit_alert_key([e1, e2]) == run._exit_alert_key([e2, e1])  # order-free
    assert run._exit_alert_key([e1, e2]) != run._exit_alert_key([e1, e2, e3])
    assert run._exit_alert_key([e1, e2]) != run._exit_alert_key([e1, e3])
    assert len(run._exit_alert_key([e1, e2, e3])) == 40  # sha1 hexdigest fits String(64)


def test_emit_exit_alert_per_event_set_idempotency(tmp_path):
    # a NEW exit event in a later hour triggers a SECOND alert (hourly cadence),
    # while a re-run with the SAME event set is a no-op.
    url = f"sqlite:///{tmp_path / 'set.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_exit_event("AMD"), _exit_event("AEP")])
        s.commit()

    sent = []

    def recorder(**kwargs):
        sent.append(kwargs)

    # event set {ev1, ev2} present -> one alert + one EmailLog(kind="exit") row
    # whose alert_key is the key over those two events.
    with Session(engine) as s:
        assert run._emit_pending_exit_alert(s, RUN, "me@example.com", recorder) is True
    assert len(sent) == 1
    with Session(engine) as s:
        evs = list(s.scalars(select(ExitEvent).order_by(ExitEvent.id)))
        logs = list(s.scalars(select(EmailLog).where(EmailLog.kind == "exit")))
    assert len(logs) == 1
    assert logs[0].alert_key == run._exit_alert_key(evs)

    # re-run with the SAME set -> NO-OP (no second send, no new log row).
    with Session(engine) as s:
        assert run._emit_pending_exit_alert(s, RUN, "me@example.com", recorder) is False
    assert len(sent) == 1
    with Session(engine) as s:
        assert len(list(s.scalars(select(EmailLog).where(EmailLog.kind == "exit")))) == 1

    # a NEW exit (ev3) fires intraday -> a SECOND alert IS sent (cadence works),
    # with a DIFFERENT alert_key.
    with Session(engine) as s:
        s.add(_exit_event("MSFT"))
        s.commit()
    with Session(engine) as s:
        assert run._emit_pending_exit_alert(s, RUN, "me@example.com", recorder) is True
    assert len(sent) == 2
    with Session(engine) as s:
        logs = list(s.scalars(select(EmailLog).where(EmailLog.kind == "exit")
                              .order_by(EmailLog.id)))
    assert len(logs) == 2
    assert logs[0].alert_key != logs[1].alert_key


def test_emit_exit_alert_tolerates_integrity_error(tmp_path, monkeypatch):
    # the SEND-then-LOG insert races the unique constraint: pre-insert the exact
    # (kind="exit", run_date, alert_key) row and bypass the pre-check, so the code
    # reaches the colliding insert. It must NOT raise and must leave the session
    # usable (the transaction is rolled back, not leaked).
    url = f"sqlite:///{tmp_path / 'race.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_exit_event("AMD"), _exit_event("AEP")])
        s.commit()
        evs = list(s.scalars(select(ExitEvent).order_by(ExitEvent.id)))
    key = run._exit_alert_key(evs)

    # pre-insert the row that the orchestrator's insert will collide with.
    with Session(engine) as s:
        s.add(EmailLog(sent_at=datetime.now(), kind="exit", subject="dup",
                       run_date=RUN, alert_key=key))
        s.commit()

    # force the code past the pre-check so it reaches the insert + commit.
    monkeypatch.setattr(run, "_exit_already_sent", lambda *a, **k: False)

    sent = []

    def recorder(**kwargs):
        sent.append(kwargs)

    with Session(engine) as s:
        # does NOT raise despite the IntegrityError on commit.
        run._emit_pending_exit_alert(s, RUN, "me@example.com", recorder)
        # the send happened first (urgent: never lose an alert).
        assert len(sent) == 1
        # session is left usable after the rollback (no leaked transaction).
        s.add(_exit_event("NVDA"))
        s.commit()  # would raise PendingRollbackError if the txn had leaked
    with Session(engine) as s:
        # still exactly one exit log row -- the collision was swallowed.
        assert len(list(s.scalars(select(EmailLog).where(EmailLog.kind == "exit")))) == 1

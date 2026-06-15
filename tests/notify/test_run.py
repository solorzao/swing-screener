from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import ExitEvent, Signal
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

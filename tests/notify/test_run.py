from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db.models import Signal
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

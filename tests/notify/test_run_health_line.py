"""send_digest pushes a one-line health footer (Phase-6 Task 4).

The health line is the always-on "is the cron alive" signal: the freshness of the latest
screen run + the money posture (execution_mode) + the autonomy-gate verdict, on one line in
both the text and HTML bodies. It is computed READ-ONLY (``latest_run_date`` + the gate's
pure SELECTs) and threaded into ``compose_digest_body`` via the additive ``health_status``
param. Unlike the gate countdown it does NOT require deep analysis -- a silently-dead cron
must be visible on every digest.

All offline: the conviction/deep analyzers and the chart/fundamentals/news seams are injected.
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db.models import Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import run

RUN = date(2026, 6, 15)


def _sig(ticker, rank):
    return Signal(run_date=RUN, ticker=ticker, timeframe="1d", horizon="medium",
                  score=1.0 / rank, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
                  entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0)


def _seed(url, n=2):
    with Session(get_engine(url)) as s:
        s.add_all([_sig(t, i + 1) for i, t in enumerate(["AMD", "AEP", "NVDA"][:n])])
        s.commit()


def _kwargs(tmp_path, url, sent, **extra):
    return dict(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                pdf_dir=tmp_path / "digests",
                smtp_send=lambda **kw: sent.append(kw), **extra)


def test_digest_body_carries_the_health_line_text_and_html(tmp_path, monkeypatch):
    # Default env: deep OFF, execution off. The health line is still pushed (always-on),
    # carrying the freshness of the latest screen run + the money posture + the gate verdict.
    monkeypatch.delenv("SWING_DEEP_ANALYSIS", raising=False)
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)
    url = f"sqlite:///{tmp_path / 'health.sqlite'}"
    _seed(url, n=2)
    sent = []

    res = run.send_digest(**_kwargs(tmp_path, url, sent))

    assert res.sent is True
    body_text = sent[-1]["text"]
    body_html = sent[-1]["html"]
    # The latest screen run is RUN (the seeded run_date); with no scored calls the gate is
    # NOT READY, and the default money posture is "off".
    assert "Health:" in body_text
    assert "2026-06-15" in body_text
    assert "execution off" in body_text
    assert "gate NOT READY" in body_text
    # surfaced in the HTML body too
    assert "Health:" in body_html
    assert "execution off" in body_html


def test_health_line_reflects_execution_mode(tmp_path, monkeypatch):
    # The money posture is always visible: arming paper mode shows "execution paper".
    monkeypatch.delenv("SWING_DEEP_ANALYSIS", raising=False)
    monkeypatch.setenv("SWING_EXECUTION_MODE", "paper")
    url = f"sqlite:///{tmp_path / 'health_paper.sqlite'}"
    _seed(url, n=2)
    sent = []

    run.send_digest(**_kwargs(tmp_path, url, sent))

    assert "execution paper" in sent[-1]["text"]

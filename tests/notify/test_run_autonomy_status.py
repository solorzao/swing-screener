"""send_digest surfaces the autonomy-gate countdown in the body footer (Phase-5 Task 4).

When deep analysis is ON (the digest already runs the analyst, so the gate's SELECTs are
free), the body footer carries the one-line ``gate_status_line`` -- the watchable countdown
toward the calibration floors. A non-deep digest is byte-for-byte unchanged: no gate line.
The gate is read-only (a pure SELECT over scored calls + the verdicts sidecars), so surfacing
it adds no writes.

All offline: the conviction/deep analyzers and the chart/fundamentals/news seams are injected.
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db.models import Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import run
from swing_screener.notify.analysis import SignalAnalysis
from swing_screener.notify.market_context import Fundamentals

RUN = date(2026, 6, 15)


def _sig(ticker, rank):
    return Signal(run_date=RUN, ticker=ticker, timeframe="1d", horizon="medium",
                  score=1.0 / rank, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
                  entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
                  chart_path=f"20260615/{ticker}_1d_20260615.png")


def _seed(url, n=2):
    with Session(get_engine(url)) as s:
        s.add_all([_sig(t, i + 1) for i, t in enumerate(["AMD", "AEP", "NVDA"][:n])])
        s.commit()


def _kwargs(tmp_path, url, sent, **extra):
    return dict(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                pdf_dir=tmp_path / "digests",
                smtp_send=lambda **kw: sent.append(kw), **extra)


def test_deep_digest_body_carries_the_gate_status_line(tmp_path, monkeypatch):
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)  # default includes daily
    url = f"sqlite:///{tmp_path / 'deep.sqlite'}"
    _seed(url, n=1)  # the single pick IS the top-N deep pick (no cheap-narration client)
    sent = []

    res = run.send_digest(**_kwargs(
        tmp_path, url, sent,
        deep_analyze_fn=lambda f, **kw: SignalAnalysis(
            core_reason=f"deep {f.ticker}", rationale="body"),
        chart_bytes_loader=lambda p: None,
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False),
        news_fn=lambda t: [], market_trend_fn=lambda: None,
        edge_dir=tmp_path / "noedge"))  # no verdicts/scored calls -> NOT READY countdown

    assert res.sent is True
    body_text = sent[-1]["text"]
    body_html = sent[-1]["html"]
    # The footer one-liner is present in both bodies; with no scored calls the gate is
    # NOT READY and shows the per-play-type countdown.
    assert "Autonomy gate: NOT READY" in body_text
    assert "continuation 0/20 high" in body_text
    assert "Autonomy gate: NOT READY" in body_html


def test_non_deep_digest_has_no_gate_status_line(tmp_path, monkeypatch):
    monkeypatch.delenv("SWING_DEEP_ANALYSIS", raising=False)  # default OFF
    url = f"sqlite:///{tmp_path / 'off.sqlite'}"
    _seed(url, n=2)
    sent = []

    res = run.send_digest(**_kwargs(tmp_path, url, sent))

    assert res.sent is True
    assert "Autonomy gate" not in sent[-1]["text"]   # non-deep digest is unchanged
    assert "Autonomy gate" not in sent[-1]["html"]


def test_deep_digest_reads_the_gate_twice_and_exit_alerts_once(tmp_path, monkeypatch):
    """Seam-count pin (2026-07-17 audit M4e): a deep digest reads the autonomy
    gate exactly TWICE -- the pre-build earned-nudge-bound snapshot, then ONE
    shared post-scoring read feeding both the countdown footer and the health
    line -- and queries pending_exit_alerts exactly ONCE (send_digest threads
    its list into _emit_pending_exit_alert instead of re-querying)."""
    from swing_screener.notify import select as sel_mod
    from swing_screener.pipeline import autonomy as autonomy_mod

    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)
    url = f"sqlite:///{tmp_path / 'count.sqlite'}"
    _seed(url, n=1)
    sent = []

    gate_calls: list[int] = []
    real_gate = autonomy_mod.autonomy_gate

    def counting_gate(*a, **kw):
        gate_calls.append(1)
        return real_gate(*a, **kw)

    monkeypatch.setattr(run, "autonomy_gate", counting_gate)

    alert_calls: list[int] = []
    real_alerts = sel_mod.pending_exit_alerts

    def counting_alerts(*a, **kw):
        alert_calls.append(1)
        return real_alerts(*a, **kw)

    monkeypatch.setattr(sel_mod, "pending_exit_alerts", counting_alerts)

    res = run.send_digest(**_kwargs(
        tmp_path, url, sent,
        deep_analyze_fn=lambda f, **kw: SignalAnalysis(
            core_reason=f"deep {f.ticker}", rationale="body"),
        chart_bytes_loader=lambda p: None,
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False),
        news_fn=lambda t: [], market_trend_fn=lambda: None,
        edge_dir=tmp_path / "noedge"))

    assert res.sent is True
    assert len(gate_calls) == 2, f"expected 2 gate reads, saw {len(gate_calls)}"
    assert len(alert_calls) == 1, f"expected 1 exit-alert query, saw {len(alert_calls)}"

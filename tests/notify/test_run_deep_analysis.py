"""send_digest deep-analysis wiring: gated by config, top-N only, seams injected.

All offline -- the deep analyzer, chart loader, and fundamentals/news fetchers
are injected, so no LLM/blob/yfinance/network is touched.
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


def _seed(url, n=4):
    with Session(get_engine(url)) as s:
        s.add_all([_sig(t, i + 1) for i, t in enumerate(["AMD", "AEP", "NVDA", "F"][:n])])
        s.commit()


def _kwargs(tmp_path, url, **extra):
    return dict(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                pdf_dir=tmp_path / "digests", smtp_send=lambda **kw: None, **extra)


def test_deep_analysis_runs_for_top_n_only_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "2")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)  # default includes daily
    url = f"sqlite:///{tmp_path / 'deep.sqlite'}"
    _seed(url, n=4)

    deep_calls, loaded, fund_calls = [], [], []

    def fake_deep(facts, **kw):
        deep_calls.append(facts.ticker)
        return SignalAnalysis(core_reason=f"deep {facts.ticker}", rationale="deep body")

    def fake_loader(path):
        loaded.append(path)
        return b"PNGBYTES"

    def fake_fund(ticker):
        fund_calls.append(ticker)
        return Fundamentals(ticker=ticker, ok=True, sector="Tech")

    res = run.send_digest(**_kwargs(
        tmp_path, url, deep_analyze_fn=fake_deep, chart_bytes_loader=fake_loader,
        fundamentals_fn=fake_fund, news_fn=lambda t: [],
        edge_dir=tmp_path))  # pin to an EMPTY edge dir: no .verdicts.json -> deep fallback,
        # independent of the repo's real edge/ contents (no hidden ambient-state dependency).

    assert res.sent is True and res.n_picks == 4
    assert deep_calls == ["AMD", "AEP"]          # only the top-2 picks
    assert fund_calls == ["AMD", "AEP"]          # fundamentals fetched for those
    assert loaded == ["20260615/AMD_1d_20260615.png", "20260615/AEP_1d_20260615.png"]


def test_deep_analysis_skipped_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv("SWING_DEEP_ANALYSIS", raising=False)  # default OFF
    url = f"sqlite:///{tmp_path / 'off.sqlite'}"
    _seed(url, n=2)

    deep_calls = []

    def fake_deep(facts, **kw):
        deep_calls.append(facts.ticker)
        return SignalAnalysis(core_reason="x", rationale="y")

    res = run.send_digest(**_kwargs(tmp_path, url, deep_analyze_fn=fake_deep))
    assert res.sent is True
    assert deep_calls == []  # master flag off -> deep analyzer never invoked


def test_deep_analysis_skipped_for_kind_not_in_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_KINDS", "weekly,monthly")  # daily excluded
    url = f"sqlite:///{tmp_path / 'scope.sqlite'}"
    _seed(url, n=2)

    deep_calls = []

    def fake_deep(facts, **kw):
        deep_calls.append(facts.ticker)
        return SignalAnalysis(core_reason="x", rationale="y")

    res = run.send_digest(**_kwargs(tmp_path, url, deep_analyze_fn=fake_deep))  # kind=daily
    assert res.sent is True
    assert deep_calls == []  # daily not in the configured deep-analysis kinds

"""SWING_EXECUTE_PLAY_TYPES dispatch scoping (Task 8): the ceremony-controlled
CEILING on which play types the dispatch loop may submit.

Continuation has NO confirmed edge (-0.161R, n=16k), so the first live agent must
trade only what the operator scopes in. Proven here, end to end through
``send_digest`` with a recording fake adapter (nothing can ever reach a venue):

* scope ``{"reversal"}`` -> the continuation intent NEVER reaches the adapter and
  gets a synthetic ``skipped`` ticket (``play type not in execution scope``) so
  the digest renders the skip honestly; the reversal intent dispatches.
* scope unset (``None``) -> BOTH dispatch: the default is byte-identical to
  today's behavior.
* an all-garbage value -> the EMPTY scope: NOTHING dispatches, both intents are
  ticketed (fail-closed -- garbage never widens scope).

All offline: conviction analyzer, chart loader, fundamentals/news, market regime
and the execution adapter are injected (the test_run_execution.py fixtures, plus a
seeded confirmed REVERSAL signal so the run builds one intent per play type).
"""

import json
from datetime import date

from swing_screener.db import guardrails_repo
from swing_screener.db.models import Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import run
from swing_screener.notify.analysis import ConvictionResult, SignalAnalysis
from swing_screener.notify.market_context import Fundamentals
from swing_screener.pipeline.execution import OrderResult
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.pipeline.reflect import Verdict
from swing_screener.settings import load_settings
from sqlalchemy.orm import Session

RUN = date(2026, 6, 15)


def _sig(ticker, rank, *, play_type="continuation", strength=None):
    return Signal(run_date=RUN, ticker=ticker, timeframe="1d", horizon="medium",
                  score=0.85, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
                  volatility_tier="med", quality_tier="high",
                  play_type=play_type, strength=strength,
                  entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
                  chart_path=f"20260615/{ticker}_1d_20260615.png")


def _seed_both(url):
    """One sized-intent candidate per play type: AMD (continuation) + XOM (a
    CONFIRMED reversal -- the default surfacing bar keeps confirmed only)."""
    with Session(get_engine(url)) as s:
        s.add_all([
            _sig("AMD", 1),
            _sig("XOM", 1, play_type="reversal", strength="confirmed"),
        ])
        s.commit()


def _edge_dir(tmp_path):
    edge = tmp_path / "edge"
    edge.mkdir()
    for pt in ("continuation", "reversal"):
        (edge / f"{pt}.md").write_text(f"## Thesis\n\n{pt} playbook.\n", encoding="utf-8")
        verdicts = [Verdict(
            play_type=pt, dimension="score", bucket="0.80-1.00", tier="forward_confirmed",
            n=40, expectancy_r=0.5, ci_low=0.2, n_clusters=8, source="forward")]
        (edge / f"{pt}.verdicts.json").write_text(
            json.dumps([v.__dict__ for v in verdicts]), encoding="utf-8")
    return edge


def _insight_kwargs(tmp_path, url, **extra):
    return dict(
        kind="daily", db_url=url, run_date=RUN, to="me@example.com",
        pdf_dir=tmp_path / "digests", smtp_send=extra.pop("smtp_send", lambda **k: None),
        analyze_conviction_fn=lambda f, **k: ConvictionResult(
            conviction="high", nudge_reason="x", insight="i", is_deep=True),
        deep_analyze_fn=lambda f, **k: SignalAnalysis(core_reason="x", rationale="y"),
        chart_bytes_loader=lambda p: None, edge_dir=_edge_dir(tmp_path),
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False), news_fn=lambda t: [],
        market_trend_fn=lambda: "bull", **extra)


class _FakeAdapter:
    """Records every submit and returns a canned OrderResult -- NEVER places an
    order or opens a position."""

    name = "fake"

    def __init__(self) -> None:
        self.result = OrderResult(
            status="recorded", account="manual", detail="order ticket recorded")
        self.calls: list[OrderIntent] = []

    def submit(self, intent, *, session, run_date, limits) -> OrderResult:
        self.calls.append(intent)
        return self.result


def _scoped_env(monkeypatch, value):
    """Deep path on (one intent per play type), sized shares, and the scope env."""
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "300")
    if value is None:
        monkeypatch.delenv("SWING_EXECUTE_PLAY_TYPES", raising=False)
    else:
        monkeypatch.setenv("SWING_EXECUTE_PLAY_TYPES", value)


def test_scope_reversal_only_skips_continuation_and_dispatches_reversal(
        tmp_path, monkeypatch):
    _scoped_env(monkeypatch, "reversal")
    url = f"sqlite:///{tmp_path / 'scope.sqlite'}"
    _seed_both(url)
    adapter = _FakeAdapter()
    sent = []

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), execution_adapter=adapter))

    assert res.sent is True
    # ONLY the reversal intent reached the adapter; continuation NEVER did.
    assert [i.ticker for i in adapter.calls] == ["XOM"]
    assert adapter.calls[0].play_type == "reversal"
    body = sent[-1]["text"].lower()
    # The out-of-scope continuation intent still renders an HONEST skipped ticket.
    assert run.OUT_OF_SCOPE_DETAIL in body
    assert "skipped" in body


def test_scope_unset_dispatches_both_play_types(tmp_path, monkeypatch):
    _scoped_env(monkeypatch, None)
    url = f"sqlite:///{tmp_path / 'unscoped.sqlite'}"
    _seed_both(url)
    adapter = _FakeAdapter()
    sent = []

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), execution_adapter=adapter))

    assert res.sent is True
    # Unset = unscoped: today's behavior, both intents dispatch (continuation first).
    assert [i.ticker for i in adapter.calls] == ["AMD", "XOM"]
    assert run.OUT_OF_SCOPE_DETAIL not in sent[-1]["text"].lower()


def test_scope_both_explicit_dispatches_both(tmp_path, monkeypatch):
    _scoped_env(monkeypatch, "continuation,reversal")
    url = f"sqlite:///{tmp_path / 'explicit.sqlite'}"
    _seed_both(url)
    adapter = _FakeAdapter()
    sent = []

    run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), execution_adapter=adapter))

    assert [i.ticker for i in adapter.calls] == ["AMD", "XOM"]
    assert run.OUT_OF_SCOPE_DETAIL not in sent[-1]["text"].lower()


def test_all_garbage_scope_dispatches_nothing_and_tickets_both(tmp_path, monkeypatch):
    """Fail-closed: an all-invalid value parses to the EMPTY scope = allow-NONE.
    Neither intent reaches the adapter; BOTH render skipped tickets."""
    _scoped_env(monkeypatch, "junk")
    url = f"sqlite:///{tmp_path / 'garbage.sqlite'}"
    _seed_both(url)
    adapter = _FakeAdapter()
    sent = []

    res = run.send_digest(**_insight_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k), execution_adapter=adapter))

    assert res.sent is True
    assert adapter.calls == []  # garbage NEVER widens scope
    body = sent[-1]["text"].lower()
    assert body.count(run.OUT_OF_SCOPE_DETAIL) == 2  # one honest ticket per intent


def test_effective_execution_scope_is_the_env_ceiling(monkeypatch):
    """The Task-22 seam: today purely the env ceiling; ``session`` is REQUIRED
    keyword-only (unused today) so no future caller can silently skip Task 22's
    cockpit subtraction by omitting it."""
    monkeypatch.setenv("SWING_EXECUTE_PLAY_TYPES", "reversal")
    s = load_settings()
    assert guardrails_repo.effective_execution_scope(
        s, session=None) == frozenset({"reversal"})
    monkeypatch.delenv("SWING_EXECUTE_PLAY_TYPES", raising=False)
    assert guardrails_repo.effective_execution_scope(
        load_settings(), session=None) is None

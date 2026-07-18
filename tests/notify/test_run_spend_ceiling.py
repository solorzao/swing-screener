"""send_digest per-run deep-analysis spend ceiling (Task 2).

A per-RUN accumulator sums each deep insight call's ``Usage.est_cost_usd``. Once it
reaches/exceeds ``SWING_DEEP_ANALYSIS_MAX_USD`` the remaining top-N picks fall back to
the cheap DETERMINISTIC narrator (no Opus call, no order intent, no AnalystCall row) --
the pick still renders, just without the conviction nudge. The cutoff logs ONCE per run.
The budget is shared across the continuation AND reversal picks (one budget per run).

Fail-safe: with no ceiling set (default None) ALL deep picks get the deep path -- today's
behavior, byte-identical. With deep OFF the ceiling is never consulted.

All offline: the conviction analyzer, the deep analyzer, the chart loader, the
fundamentals/news fetchers, and the market-regime are injected.
"""

import json
import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import AnalystCall, Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import run
from swing_screener.notify.analysis import ConvictionResult, SignalAnalysis, Usage
from swing_screener.notify.market_context import Fundamentals
from swing_screener.pipeline.reflect import Verdict

RUN = date(2026, 6, 15)


def _sig(ticker, rank):
    return Signal(run_date=RUN, ticker=ticker, timeframe="1d", horizon="medium",
                  score=0.85, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
                  volatility_tier="med", quality_tier="high",
                  entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
                  chart_path=f"20260615/{ticker}_1d_20260615.png")


def _seed(url, tickers):
    with Session(get_engine(url)) as s:
        s.add_all([_sig(t, i + 1) for i, t in enumerate(tickers)])
        s.commit()


def _edge_dir(tmp_path, *, play_types=("continuation", "reversal")):
    """A temp edge dir with a playbook + verdicts sidecar for each play type so the
    insight engine (conviction path) runs for those picks."""
    edge = tmp_path / "edge"
    edge.mkdir()
    for pt in play_types:
        (edge / f"{pt}.md").write_text(f"## Thesis\n\n{pt} playbook.\n", encoding="utf-8")
        verdicts = [Verdict(
            play_type=pt, dimension="score", bucket="0.80-1.00", tier="forward_confirmed",
            n=40, expectancy_r=0.5, ci_low=0.2, n_clusters=8, source="forward",
        )]
        (edge / f"{pt}.verdicts.json").write_text(
            json.dumps([v.__dict__ for v in verdicts]), encoding="utf-8")
    return edge


def _kwargs(tmp_path, url, **extra):
    return dict(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                pdf_dir=tmp_path / "digests", smtp_send=extra.pop("smtp_send", lambda **k: None),
                **extra)


def _conv_fake(conv_calls, *, cost):
    """A fake conviction analyst that records each call and reports a known est_cost."""
    def fake_conv(facts, *, baseline, **kw):
        conv_calls.append(facts.ticker)
        return ConvictionResult(
            conviction="high", nudge_reason="x", insight="i", is_deep=True,
            usage=Usage(input_tokens=0, output_tokens=0, web_searches=0, est_cost_usd=cost))
    return fake_conv


def test_ceiling_trips_after_first_pick_remaining_use_deterministic(tmp_path, monkeypatch, caplog):
    # Ceiling 0.50; each deep call costs 0.40 -> after the 1st call the accumulator (0.40)
    # is still under, the 2nd runs (0.80) crosses it, the 3rd+ skip to deterministic.
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_KINDS", "daily")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "5")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_MAX_USD", "0.50")
    url = f"sqlite:///{tmp_path / 'ceiling.sqlite'}"
    _seed(url, ["AMD", "AEP", "NVDA", "INTC"])
    edge = _edge_dir(tmp_path, play_types=("continuation",))  # only continuation playbook

    conv_calls, deep_calls = [], []
    with caplog.at_level(logging.WARNING):
        res = run.send_digest(**_kwargs(
            tmp_path, url,
            analyze_conviction_fn=_conv_fake(conv_calls, cost=0.40),
            deep_analyze_fn=lambda f, **k: (deep_calls.append(f.ticker) or
                                            SignalAnalysis(core_reason="d", rationale="r")),
            chart_bytes_loader=lambda p: None, edge_dir=edge,
            fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False), news_fn=lambda t: [],
            market_trend_fn=lambda: "bull"))

    assert res.sent is True
    # Deep conviction ran only until the ceiling: AMD (acc 0.40), AEP (acc 0.80 >= 0.50);
    # NVDA/INTC fall back to the deterministic narrator (no deep call).
    assert conv_calls == ["AMD", "AEP"]
    assert deep_calls == []  # continuation has a playbook -> insight engine, never legacy deep
    # Only the two deep picks wrote an AnalystCall row; the deterministic picks did not.
    with Session(get_engine(url)) as s:
        tickers = sorted(c.ticker for c in s.scalars(select(AnalystCall)))
        assert tickers == ["AEP", "AMD"]
    # The cutoff is logged exactly once per run.
    cutoffs = [r for r in caplog.records if "spend ceiling" in r.getMessage()]
    assert len(cutoffs) == 1


def test_ceiling_budget_shared_across_continuation_and_reversal(tmp_path, monkeypatch, caplog):
    # ONE budget for the whole run: a continuation pick spends it, so the reversal picks
    # (a separate _build_picks call) fall back to deterministic -- proves it's per-RUN.
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_KINDS", "daily")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "5")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_MAX_USD", "0.50")
    url = f"sqlite:///{tmp_path / 'shared.sqlite'}"
    _seed(url, ["AMD"])  # one continuation pick; reversal picks come from sel.reversal_picks
    edge = _edge_dir(tmp_path)  # both playbooks present

    conv_calls = []

    def fake_reversal(session, run_date, **kw):  # one reversal candidate, distinct ticker
        return [_sig("REVX", 1)]

    monkeypatch.setattr(run.sel, "reversal_picks", fake_reversal)

    with caplog.at_level(logging.WARNING):
        run.send_digest(**_kwargs(
            tmp_path, url,
            analyze_conviction_fn=_conv_fake(conv_calls, cost=0.60),  # one call exhausts 0.50
            deep_analyze_fn=lambda f, **k: SignalAnalysis(core_reason="d", rationale="r"),
            chart_bytes_loader=lambda p: None, edge_dir=edge,
            fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False), news_fn=lambda t: [],
            market_trend_fn=lambda: "bull"))

    # The continuation pick spent the budget; the reversal pick saw the ceiling already
    # reached and used the deterministic path -> only AMD got a deep conviction call.
    assert conv_calls == ["AMD"]
    with Session(get_engine(url)) as s:
        assert sorted(c.ticker for c in s.scalars(select(AnalystCall))) == ["AMD"]
    cutoffs = [r for r in caplog.records if "spend ceiling" in r.getMessage()]
    assert len(cutoffs) == 1  # logged ONCE for the whole run, not once per play type


class _EmptyBilledClient:
    """A fake anthropic client whose every call is BILLED (usage present) but
    returns no usable text -- the shape of a persistent failure mode (E3b)."""

    def __init__(self, in_tokens):
        self._in = in_tokens

    @property
    def messages(self):
        outer = self

        class _Text:
            type = "text"
            text = ""
            citations = ()

        class _Usage:
            input_tokens = outer._in
            output_tokens = 0
            server_tool_use = None

        class _Resp:
            content = [_Text()]
            usage = _Usage()

        class _M:
            def create(self, **kw):
                return _Resp()

        return _M()


def test_ceiling_charges_billed_but_failed_calls(tmp_path, monkeypatch, caplog):
    # E3b: an "empty-response day" -- every analyst call is BILLED but yields no
    # usable text, so the REAL analyze_conviction returns its deterministic
    # fallback. The fallback carries the captured usage, so spend accumulates and
    # the ceiling still engages instead of failing open exactly when things break.
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_KINDS", "daily")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "5")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_MAX_USD", "0.50")
    monkeypatch.setenv("SWING_ANALYSIS_MODEL", "claude-opus-4-8")
    url = f"sqlite:///{tmp_path / 'billedfail.sqlite'}"
    _seed(url, ["AMD", "AEP", "NVDA", "INTC"])
    edge = _edge_dir(tmp_path, play_types=("continuation",))

    # 80k input tokens at opus $5/MTok == $0.40 per billed-but-empty call: AMD
    # (acc 0.40) is under the $0.50 ceiling, AEP (acc 0.80) crosses it, NVDA/INTC
    # must skip the analyst entirely.
    with caplog.at_level(logging.WARNING):
        res = run.send_digest(**_kwargs(
            tmp_path, url, anthropic_client=_EmptyBilledClient(in_tokens=80_000),
            deep_analyze_fn=lambda f, **k: SignalAnalysis(core_reason="d", rationale="r"),
            chart_bytes_loader=lambda p: None, edge_dir=edge,
            fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False), news_fn=lambda t: [],
            market_trend_fn=lambda: "bull"))

    assert res.sent is True
    # Only the two picks that ran before the ceiling wrote an AnalystCall row.
    with Session(get_engine(url)) as s:
        assert sorted(c.ticker for c in s.scalars(select(AnalystCall))) == ["AEP", "AMD"]
    cutoffs = [r for r in caplog.records if "spend ceiling" in r.getMessage()]
    assert len(cutoffs) == 1


def test_no_ceiling_runs_all_deep_picks(tmp_path, monkeypatch, caplog):
    # Default (no SWING_DEEP_ANALYSIS_MAX_USD) -> the ceiling never fires; ALL top-N picks
    # get the deep path even when each reports a cost. Regression: byte-identical to today.
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_KINDS", "daily")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "5")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_MAX_USD", raising=False)
    url = f"sqlite:///{tmp_path / 'noceiling.sqlite'}"
    _seed(url, ["AMD", "AEP", "NVDA"])
    edge = _edge_dir(tmp_path, play_types=("continuation",))

    conv_calls = []
    with caplog.at_level(logging.WARNING):
        run.send_digest(**_kwargs(
            tmp_path, url,
            analyze_conviction_fn=_conv_fake(conv_calls, cost=99.0),  # huge cost, but no ceiling
            deep_analyze_fn=lambda f, **k: SignalAnalysis(core_reason="d", rationale="r"),
            chart_bytes_loader=lambda p: None, edge_dir=edge,
            fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False), news_fn=lambda t: [],
            market_trend_fn=lambda: "bull"))

    assert conv_calls == ["AMD", "AEP", "NVDA"]  # every top-N pick ran deep
    with Session(get_engine(url)) as s:
        assert len(list(s.scalars(select(AnalystCall)))) == 3
    assert [r for r in caplog.records if "spend ceiling" in r.getMessage()] == []

"""send_digest insight-engine wiring (Task 6, Part B).

When deep analysis is ON *and* a play type has a playbook + verdicts sidecar, the
top-N deep picks go through the INSIGHT ENGINE (one Opus conviction call -- NOT the
old deep_analyze) and the digest renders an order intent (conviction + sized shares
+ levels), writes an AnalystCall row, and scores any resolved prior calls. With NO
playbook present the digest falls back to the old deep path unchanged.

All offline: the conviction analyzer, the deep analyzer, the chart loader, the
fundamentals/news fetchers, and the market-regime are injected, so no LLM / blob /
yfinance / network is touched.
"""

import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import AnalystCall, PaperTrade, Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import run
from swing_screener.notify.analysis import ConvictionResult, SignalAnalysis
from swing_screener.notify.market_context import Fundamentals
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.reflect import Verdict
from swing_screener.pipeline.variants import DEFAULT_VARIANT

RUN = date(2026, 6, 15)


def _sig(ticker, rank):
    return Signal(run_date=RUN, ticker=ticker, timeframe="1d", horizon="medium",
                  score=0.85, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
                  volatility_tier="med", quality_tier="high",
                  entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
                  chart_path=f"20260615/{ticker}_1d_20260615.png")


def _seed(url, n=2):
    with Session(get_engine(url)) as s:
        s.add_all([_sig(t, i + 1) for i, t in enumerate(["AMD", "AEP", "NVDA"][:n])])
        s.commit()


def _edge_dir(tmp_path, *, play_types=("continuation", "reversal")):
    """A temp edge dir with a playbook + verdicts sidecar for each play type. The
    verdicts include a forward_confirmed score=0.80-1.00 cell so the baseline -> high."""
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


def test_insight_engine_renders_order_intent_and_records_call(tmp_path, monkeypatch):
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "300")  # -> sized shares
    url = f"sqlite:///{tmp_path / 'insight.sqlite'}"
    _seed(url, n=2)
    edge = _edge_dir(tmp_path)

    conv_calls, deep_calls = [], []
    sent = []

    def fake_conv(facts, *, baseline, **kw):
        conv_calls.append((facts.ticker, baseline))
        return ConvictionResult(
            conviction="high", nudge_reason="sector momentum confirms",
            insight="Strong continuation; key risk earnings.", is_deep=True)

    def fake_deep(facts, **kw):  # must NOT be called for picks with a playbook
        deep_calls.append(facts.ticker)
        return SignalAnalysis(core_reason="x", rationale="y")

    res = run.send_digest(**_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k),
        analyze_conviction_fn=fake_conv, deep_analyze_fn=fake_deep,
        chart_bytes_loader=lambda p: b"PNG", edge_dir=edge,
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=True, sector="Tech"),
        news_fn=lambda t: [], market_trend_fn=lambda: "bull"))

    assert res.sent is True
    # ONE conviction call for the single top pick; deep_analyze was NOT also called (no double-bill).
    assert conv_calls == [("AMD", "high")]   # baseline high from the forward_confirmed verdict
    assert deep_calls == []
    # The order intent is rendered in the body: conviction + sized shares.
    body = sent[-1]["text"]
    assert "high" in body.lower()
    assert "shares" in body.lower()
    # An AnalystCall row was written for the pick.
    with Session(get_engine(url)) as s:
        rows = list(s.scalars(select(AnalystCall)))
        assert len(rows) == 1
        assert rows[0].ticker == "AMD"
        assert rows[0].baseline_conviction == "high"
        assert rows[0].final_conviction == "high"
        assert rows[0].play_type == "continuation"


def test_edge_dir_resolves_from_env_when_not_passed(tmp_path, monkeypatch):
    """With no explicit edge_dir, send_digest must resolve it from SWING_EDGE_DIR (settings)
    rather than a cwd-relative Path("edge") -- the container has no /app/edge unless the
    env/default points at the shipped copy, and the cwd-relative default is what silently
    disabled the insight engine in prod (2026-07-01 audit)."""
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)
    url = f"sqlite:///{tmp_path / 'envedge.sqlite'}"
    _seed(url, n=1)
    edge = _edge_dir(tmp_path)
    monkeypatch.setenv("SWING_EDGE_DIR", str(edge))

    conv_calls = []
    run.send_digest(**_kwargs(  # NOTE: no edge_dir= -- must come from the env
        tmp_path, url,
        analyze_conviction_fn=lambda f, **k: (conv_calls.append(f.ticker) or ConvictionResult(
            conviction="high", nudge_reason="x", insight="i", is_deep=True)),
        chart_bytes_loader=lambda p: None,
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False),
        news_fn=lambda t: [], market_trend_fn=lambda: None))

    assert conv_calls == ["AMD"]  # insight engine engaged via the env-resolved edge dir
    with Session(get_engine(url)) as s:
        assert len(list(s.scalars(select(AnalystCall)))) == 1


def test_missing_playbooks_warn_loudly_when_deep_on(tmp_path, monkeypatch, caplog):
    """Deep analysis ON + no playbook/verdicts -> a WARNING naming each play type. The
    silent fallback is what hid the dead learning loop for weeks: Opus was billed daily
    while record_analyst_call was unreachable, with nothing in the logs."""
    import logging

    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)
    url = f"sqlite:///{tmp_path / 'noedge.sqlite'}"
    _seed(url, n=1)
    empty_edge = tmp_path / "no_such_edge"

    with caplog.at_level(logging.WARNING, logger="swing_screener.notify.run"):
        run.send_digest(**_kwargs(
            tmp_path, url,
            deep_analyze_fn=lambda facts, **k: SignalAnalysis(core_reason="x", rationale="y"),
            chart_bytes_loader=lambda p: None, edge_dir=empty_edge,
            fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False),
            news_fn=lambda t: [], market_trend_fn=lambda: None))

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("insight engine" in m and "continuation" in m for m in warnings)
    assert any("insight engine" in m and "reversal" in m for m in warnings)


def test_missing_playbooks_surface_in_digest_footer(tmp_path, monkeypatch):
    """The insight-OFF state must reach the EMAIL, not just a container log line -- an
    unread log is the channel that hid the 2026-07 outage for weeks. With deep on and no
    playbooks, the digest footer says so; with playbooks present, no such note."""
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)
    url = f"sqlite:///{tmp_path / 'footer.sqlite'}"
    _seed(url, n=1)
    sent = []
    run.send_digest(**_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k),
        deep_analyze_fn=lambda facts, **k: SignalAnalysis(core_reason="x", rationale="y"),
        chart_bytes_loader=lambda p: None, edge_dir=tmp_path / "no_such_edge",
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False),
        news_fn=lambda t: [], market_trend_fn=lambda: None))
    body = sent[-1]["text"]
    assert "insight engine OFF" in body
    assert "continuation" in body and "reversal" in body


def test_scoring_runs_even_with_deep_analysis_off(tmp_path, monkeypatch):
    """score_analyst_calls is a cheap idempotent DB join, not a model call -- gating it
    behind SWING_DEEP_ANALYSIS meant pausing the analyst silently froze the grading of
    calls ALREADY MADE (2026-07 audit). Scoring must run on every daily digest."""
    monkeypatch.delenv("SWING_DEEP_ANALYSIS", raising=False)  # deep path OFF
    url = f"sqlite:///{tmp_path / 'scoreoff.sqlite'}"
    _seed(url, n=1)
    with Session(get_engine(url)) as s:
        s.add(AnalystCall(
            created_date=date(2026, 6, 1), ticker="OLD", timeframe="1d",
            play_type="continuation", run_date=date(2026, 6, 1),
            baseline_conviction="medium", final_conviction="high", nudge_reason="x",
            model="claude-opus-4-8"))
        s.add(PaperTrade(
            ticker="OLD", timeframe="1d", horizon="medium", play_type="continuation",
            signal_score=0.8, rank=1, arm=BASELINE, variant=DEFAULT_VARIANT,
            fill_status="filled", stop=95.0, target=110.0, risk=5.0, status="closed",
            realized_r=1.5, hold_bars=3, opened_date=date(2026, 6, 2),
            exit_date=date(2026, 6, 9)))
        s.commit()

    run.send_digest(**_kwargs(tmp_path, url))

    with Session(get_engine(url)) as s:
        old = s.scalars(select(AnalystCall).where(AnalystCall.ticker == "OLD")).one()
        assert old.realized_r == 1.5  # graded despite the analyst being off


def test_insight_engine_scores_resolved_prior_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)
    url = f"sqlite:///{tmp_path / 'score.sqlite'}"
    _seed(url, n=1)
    edge = _edge_dir(tmp_path)

    # A prior unscored AnalystCall + the resolving closed paper trade (opened AFTER the call).
    with Session(get_engine(url)) as s:
        s.add(AnalystCall(
            created_date=date(2026, 6, 1), ticker="OLD", timeframe="1d",
            play_type="continuation", run_date=date(2026, 6, 1),
            baseline_conviction="medium", final_conviction="high", nudge_reason="x",
            model="claude-opus-4-8"))
        s.add(PaperTrade(
            ticker="OLD", timeframe="1d", horizon="medium", play_type="continuation",
            signal_score=0.8, rank=1, arm=BASELINE, variant=DEFAULT_VARIANT,
            fill_status="filled", stop=95.0, target=110.0, risk=5.0, status="closed",
            realized_r=1.5, hold_bars=3, opened_date=date(2026, 6, 2),
            exit_date=date(2026, 6, 9)))
        s.commit()

    run.send_digest(**_kwargs(
        tmp_path, url, analyze_conviction_fn=lambda f, **k: ConvictionResult(
            conviction="high", nudge_reason="x", insight="i", is_deep=True),
        chart_bytes_loader=lambda p: None, edge_dir=edge,
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False),
        news_fn=lambda t: [], market_trend_fn=lambda: None))

    with Session(get_engine(url)) as s:
        old = s.scalars(select(AnalystCall).where(AnalystCall.ticker == "OLD")).one()
        assert old.realized_r == 1.5           # the prior call got scored this run
        assert old.scored_at == date(2026, 6, 9)


def _seed_calibrated_continuation_calls(url):
    """Seed deep, well-clustered SCORED continuation calls where high CLEARLY out-earns low,
    so ``conviction_calibrated(play_type='continuation')`` certifies -> max_step=2. Reversal
    gets none, so it stays uncalibrated -> max_step=1."""
    with Session(get_engine(url)) as s:
        for i in range(8):  # 8 distinct tickers per bucket, 5 calls each (n=40, clusters=8)
            for r in (1.2, 1.4, 1.3, 1.2, 1.4):
                s.add(AnalystCall(
                    created_date=date(2026, 6, 1), ticker=f"H{i}", timeframe="1d",
                    play_type="continuation", run_date=date(2026, 6, 1),
                    baseline_conviction="medium", final_conviction="high",
                    nudge_reason="x", model="claude-opus-4-8",
                    realized_r=r, scored_at=date(2026, 6, 5)))
            for r in (-0.1, 0.0, -0.2, 0.1, -0.1):
                s.add(AnalystCall(
                    created_date=date(2026, 6, 1), ticker=f"L{i}", timeframe="1d",
                    play_type="continuation", run_date=date(2026, 6, 1),
                    baseline_conviction="medium", final_conviction="low",
                    nudge_reason="x", model="claude-opus-4-8",
                    realized_r=r, scored_at=date(2026, 6, 5)))
        s.commit()


def test_calibrated_play_type_earns_max_step_two_uncalibrated_stays_one(tmp_path, monkeypatch):
    # A play type whose scored calls calibrate gets max_step=2 (the analyst's +2 nudge
    # survives); an uncalibrated play type gets max_step=1. The gate's calib is computed
    # per play type and recomputed every run (reversible).
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)
    url = f"sqlite:///{tmp_path / 'earned.sqlite'}"
    _seed(url, n=1)
    _seed_calibrated_continuation_calls(url)
    edge = _edge_dir(tmp_path)

    seen = {}  # ticker -> max_step passed to the conviction analyst

    def fake_conv(facts, *, baseline, max_step=1, **kw):
        seen[facts.ticker] = max_step
        return ConvictionResult(
            conviction="high", nudge_reason="r", insight="i", is_deep=True)

    res = run.send_digest(**_kwargs(
        tmp_path, url, analyze_conviction_fn=fake_conv,
        chart_bytes_loader=lambda p: None, edge_dir=edge,
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False), news_fn=lambda t: [],
        market_trend_fn=lambda: "bull"))

    assert res.sent is True
    # The continuation pick (AMD) calibrates -> max_step=2.
    assert seen["AMD"] == 2
    # The reversal Top-5 (daily) has NO scored reversal calls -> uncalibrated -> max_step=1.
    # Reversal picks may be empty (no reversal signals seeded); if any ran, they saw step 1.
    reversal_steps = [v for k, v in seen.items() if k != "AMD"]
    assert all(v == 1 for v in reversal_steps)


def test_uncalibrated_continuation_keeps_max_step_one(tmp_path, monkeypatch):
    # With NO scored calls, continuation cannot calibrate -> max_step stays 1 (byte-identical
    # to today's hard ±1 clamp). The earned bound never widens without a certified track record.
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)
    url = f"sqlite:///{tmp_path / 'uncal.sqlite'}"
    _seed(url, n=1)
    edge = _edge_dir(tmp_path)

    seen = []

    def fake_conv(facts, *, baseline, max_step=1, **kw):
        seen.append(max_step)
        return ConvictionResult(conviction="high", nudge_reason="r", insight="i", is_deep=True)

    run.send_digest(**_kwargs(
        tmp_path, url, analyze_conviction_fn=fake_conv,
        chart_bytes_loader=lambda p: None, edge_dir=edge,
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False), news_fn=lambda t: [],
        market_trend_fn=lambda: "bull"))

    assert seen and all(v == 1 for v in seen)  # no track record -> hard ±1


def test_falls_back_to_deep_path_when_no_playbook(tmp_path, monkeypatch):
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)
    url = f"sqlite:///{tmp_path / 'nopb.sqlite'}"
    _seed(url, n=1)
    empty_edge = tmp_path / "edge"          # exists but no playbook/verdicts files
    empty_edge.mkdir()

    conv_calls, deep_calls = [], []

    res = run.send_digest(**_kwargs(
        tmp_path, url,
        analyze_conviction_fn=lambda f, **k: (conv_calls.append(f.ticker) or  # type: ignore
                                              ConvictionResult("high", "x", "i", True)),
        deep_analyze_fn=lambda f, **k: (deep_calls.append(f.ticker) or
                                        SignalAnalysis(core_reason="deep", rationale="b")),
        chart_bytes_loader=lambda p: None, edge_dir=empty_edge,
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=False), news_fn=lambda t: [],
        market_trend_fn=lambda: "bull"))

    assert res.sent is True
    assert deep_calls == ["AMD"]    # fell back to the old deep path
    assert conv_calls == []         # insight engine NOT used (no playbook)
    with Session(get_engine(url)) as s:
        assert list(s.scalars(select(AnalystCall))) == []  # no AnalystCall written

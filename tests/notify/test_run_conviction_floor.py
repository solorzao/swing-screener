"""send_digest conviction FLOOR: only medium/high plays surface, and picks that could
never clear the floor never bill a conviction call.

Two halves of one rule, both reading ``StrategyConfig.min_conviction``:

* SPEND GATE -- ``conviction_baseline`` returns avoid/medium/high (never "low"), so under
  the hard +-1 clamp an "avoid" baseline tops out at "low" and can NEVER be surfaced.
  Paying Opus to grade it is waste, so the call is skipped entirely.
* DISPLAY FILTER -- a pick graded below the floor (or never graded at all) is dropped from
  the body, the PDF, and the dispatched intents.

An emptied list must still say WHY (the conviction attribution line): a quiet market and
a broken playbook must never produce the same silent empty email.

All offline: conviction analyzer, chart loader, fundamentals/news, and market regime are
injected, so no LLM / blob / yfinance / network is touched.
"""

import json
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import AnalystCall, Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import run
from swing_screener.notify.analysis import ConvictionResult
from swing_screener.notify.market_context import Fundamentals
from swing_screener.pipeline.reflect import Verdict

RUN = date(2026, 6, 15)


@pytest.fixture(autouse=True)
def _unpark(unpark_continuation):
    """Drive the floor THROUGH continuation picks -- parked by default since the Q6 NULL
    (see tests/notify/conftest.py); un-park so these tests exercise the filter."""


def _sig(ticker, rank):
    return Signal(run_date=RUN, ticker=ticker, timeframe="1d", horizon="medium",
                  score=0.85, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
                  volatility_tier="med", quality_tier="high",
                  entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
                  chart_path=f"20260615/{ticker}_1d_20260615.png")


def _seed(url, n=1):
    with Session(get_engine(url)) as s:
        s.add_all([_sig(t, i + 1) for i, t in enumerate(["AMD", "AEP", "NVDA"][:n])])
        s.commit()


def _edge_dir(tmp_path, *, ci_low=0.2, play_types=("continuation", "reversal")):
    """Edge dir whose score=0.80-1.00 cell is forward_confirmed. ``ci_low`` picks the
    baseline the seeded score=0.85 picks resolve to: positive -> "high", NEGATIVE -> the
    "avoid" baseline (a confirmed cell whose lower bound is below zero)."""
    edge = tmp_path / "edge"
    edge.mkdir()
    for pt in play_types:
        (edge / f"{pt}.md").write_text(f"## Thesis\n\n{pt} playbook.\n", encoding="utf-8")
        verdicts = [Verdict(
            play_type=pt, dimension="score", bucket="0.80-1.00", tier="forward_confirmed",
            n=40, expectancy_r=0.5, ci_low=ci_low, n_clusters=8, source="forward",
        )]
        (edge / f"{pt}.verdicts.json").write_text(
            json.dumps([v.__dict__ for v in verdicts]), encoding="utf-8")
    return edge


def _kwargs(tmp_path, url, **extra):
    return dict(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                pdf_dir=tmp_path / "digests",
                smtp_send=extra.pop("smtp_send", lambda **k: None), **extra)


def _run(tmp_path, url, edge, conv_fn, sent, **extra):
    return run.send_digest(**_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k),
        analyze_conviction_fn=conv_fn, chart_bytes_loader=lambda p: b"PNG", edge_dir=edge,
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=True, sector="Tech"),
        news_fn=lambda t: [], market_trend_fn=lambda: "bull", **extra))


@pytest.fixture(autouse=True)
def _deep_on(monkeypatch):
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "1")
    monkeypatch.delenv("SWING_DEEP_ANALYSIS_KINDS", raising=False)
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "300")


# ---------------------------------------------------------------------------
# The spend gate.
# ---------------------------------------------------------------------------
def test_avoid_baseline_never_bills_a_conviction_call(tmp_path):
    """THE saving. A confirmed-but-negative playbook cell grades the pick "avoid"; under
    the +-1 clamp its best reachable grade is "low", which can never clear the medium
    floor -- so no Opus call is made and no AnalystCall row is written."""
    url = f"sqlite:///{tmp_path / 'avoid.sqlite'}"
    _seed(url, n=1)
    edge = _edge_dir(tmp_path, ci_low=-0.1)  # forward_confirmed AND negative -> avoid

    conv_calls, sent = [], []

    def fake_conv(facts, *, baseline, **kw):
        conv_calls.append((facts.ticker, baseline))
        return ConvictionResult(conviction="low", nudge_reason="", insight="", is_deep=True)

    res = _run(tmp_path, url, edge, fake_conv, sent)

    assert res.sent is True
    assert conv_calls == []                      # the call was SKIPPED, not made-then-filtered
    assert "AMD" not in sent[-1]["text"]         # and the pick never surfaced
    with Session(get_engine(url)) as s:
        assert list(s.scalars(select(AnalystCall))) == []


def test_avoid_baseline_is_analyzed_when_the_play_type_earned_a_two_step_nudge(tmp_path,
                                                                               monkeypatch):
    """The gate must follow the EARNED bound, not a hard-coded 1. A play type certified
    for +-2 can lift "avoid" to "medium", so its picks become reachable and must resume
    being analyzed -- automatically, with no constant to keep in sync."""
    monkeypatch.setattr(run, "max_conviction_step", lambda _calib: 2)
    url = f"sqlite:///{tmp_path / 'avoid2.sqlite'}"
    _seed(url, n=1)
    edge = _edge_dir(tmp_path, ci_low=-0.1)

    conv_calls, sent = [], []

    def fake_conv(facts, *, baseline, **kw):
        conv_calls.append((facts.ticker, baseline))
        return ConvictionResult(conviction="medium", nudge_reason="vol dry-up",
                                insight="Reachable at +-2.", is_deep=True)

    _run(tmp_path, url, edge, fake_conv, sent)

    assert conv_calls == [("AMD", "avoid")]      # reachable now -> the call goes through
    assert "AMD" in sent[-1]["text"]             # and medium clears the floor


# ---------------------------------------------------------------------------
# The display filter.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("graded", "surfaced"), [
    ("high", True),
    ("medium", True),     # the floor itself is inclusive
    ("low", False),
    ("avoid", False),
])
def test_only_picks_at_or_above_the_floor_surface(tmp_path, graded, surfaced):
    url = f"sqlite:///{tmp_path / f'grade_{graded}.sqlite'}"
    _seed(url, n=1)
    edge = _edge_dir(tmp_path)  # baseline high

    sent = []

    def fake_conv(facts, **kw):
        return ConvictionResult(conviction=graded, nudge_reason="r", insight="i",
                                is_deep=True)

    _run(tmp_path, url, edge, fake_conv, sent)

    assert ("AMD" in sent[-1]["text"]) is surfaced


def test_a_paid_but_filtered_call_is_still_recorded_for_learning(tmp_path):
    """A "low" grade cost real money and is real evidence: the AnalystCall row MUST still
    be written and scored, or the calibration test loses the "low" bucket it needs to ever
    certify a play type for the wider nudge."""
    url = f"sqlite:///{tmp_path / 'learn.sqlite'}"
    _seed(url, n=1)
    edge = _edge_dir(tmp_path)

    sent = []

    def fake_conv(facts, **kw):
        return ConvictionResult(conviction="low", nudge_reason="thin volume",
                                insight="Weak.", is_deep=True)

    _run(tmp_path, url, edge, fake_conv, sent)

    assert "AMD" not in sent[-1]["text"]         # filtered from the digest
    with Session(get_engine(url)) as s:
        rows = list(s.scalars(select(AnalystCall)))
        assert len(rows) == 1                    # but kept as evidence
        assert rows[0].final_conviction == "low"


def test_ungraded_picks_are_dropped_when_deep_analysis_is_off(tmp_path, monkeypatch):
    """Deep analysis off -> nothing carries a conviction, so nothing clears the floor.
    Honors "even if it means we don't have any on some days" rather than failing open."""
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "0")
    url = f"sqlite:///{tmp_path / 'off.sqlite'}"
    _seed(url, n=2)
    edge = _edge_dir(tmp_path)

    sent = []
    _run(tmp_path, url, edge, lambda *a, **k: None, sent)

    body = sent[-1]["text"]
    assert "AMD" not in body and "AEP" not in body


def test_a_missing_playbook_bills_nothing_at_all(tmp_path):
    """A play type with no playbook can only ever produce UNGRADED picks, which the floor
    drops -- so the legacy deep fallback AND the narrator must both be skipped. Without
    this, a missing sidecar would bill Opus for every pick and then throw all of it away:
    the 2026-07 freeze, but paying for the privilege. The outage is now cheap AND loud
    (the existing playbook warning plus the attribution line)."""
    url = f"sqlite:///{tmp_path / 'nopb.sqlite'}"
    _seed(url, n=2)
    empty_edge = tmp_path / "edge"               # exists, but no playbook/verdicts files
    empty_edge.mkdir()

    deep_calls, narr_calls, sent = [], [], []

    class _FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                narr_calls.append(kw)
                raise AssertionError("the narrator must not be billed for a dropped pick")

    def fake_deep(facts, **kw):
        deep_calls.append(facts.ticker)
        raise AssertionError("the deep fallback must not be billed for a dropped pick")

    res = run.send_digest(**_kwargs(
        tmp_path, url, smtp_send=lambda **k: sent.append(k),
        deep_analyze_fn=fake_deep, anthropic_client=_FakeClient(),
        chart_bytes_loader=lambda p: b"PNG", edge_dir=empty_edge,
        fundamentals_fn=lambda t: Fundamentals(ticker=t, ok=True, sector="Tech"),
        news_fn=lambda t: [], market_trend_fn=lambda: "bull"))

    assert res.sent is True                      # the email still goes out
    assert deep_calls == [] and narr_calls == []  # having cost nothing
    assert "2 skipped" in sent[-1]["text"]       # and saying so


def test_picks_beyond_top_n_are_dropped_as_ungraded(tmp_path):
    """Only the top-N are graded; the rest get deterministic narration and no conviction.
    Those must not surface either -- an ungraded pick is exactly the un-vetted noise the
    floor removes."""
    url = f"sqlite:///{tmp_path / 'topn.sqlite'}"
    _seed(url, n=3)                              # top_n=1 -> AEP/NVDA are never graded
    edge = _edge_dir(tmp_path)

    sent = []

    def fake_conv(facts, **kw):
        return ConvictionResult(conviction="high", nudge_reason="r", insight="i",
                                is_deep=True)

    _run(tmp_path, url, edge, fake_conv, sent)

    body = sent[-1]["text"]
    assert "AMD" in body                         # the graded top pick
    assert "AEP" not in body and "NVDA" not in body


# ---------------------------------------------------------------------------
# Anti-silence: an empty list must say WHY.
# ---------------------------------------------------------------------------
def test_filtered_empty_list_explains_itself(tmp_path):
    """The whole list filtered away must NOT read like a quiet market. The attribution
    line names each stage so a broken playbook is visibly different from no setups."""
    url = f"sqlite:///{tmp_path / 'empty.sqlite'}"
    _seed(url, n=1)
    edge = _edge_dir(tmp_path)

    sent = []

    def fake_conv(facts, **kw):
        return ConvictionResult(conviction="low", nudge_reason="r", insight="i",
                                is_deep=True)

    _run(tmp_path, url, edge, fake_conv, sent)

    body = sent[-1]["text"]
    assert "Conviction:" in body
    assert "1 graded" in body
    assert "1 below medium" in body
    assert "0 surfaced" in body


def test_attribution_line_counts_the_skipped_avoid_baselines(tmp_path):
    """A pick skipped BEFORE the call is a different story from one graded and filtered --
    the line must distinguish them, so a playbook that has turned hostile is visible."""
    url = f"sqlite:///{tmp_path / 'skipped.sqlite'}"
    _seed(url, n=1)
    edge = _edge_dir(tmp_path, ci_low=-0.1)      # avoid baseline -> skipped, never graded

    sent = []
    _run(tmp_path, url, edge, lambda *a, **k: None, sent)

    body = sent[-1]["text"]
    assert "1 skipped" in body
    assert "0 surfaced" in body

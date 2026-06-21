"""Tests for the conviction-nudge seam (Task 3).

All offline: a recording fake client captures the messages.create kwargs so we
can assert the prompt shape (baseline, playbook, the +-1 rule) without any
network, and a boom client proves the graceful baseline fallback.

The load-bearing assertion: the +-1 bound is enforced in CODE -- a model that
returns a grade beyond +-1 of the baseline MUST be clamped, never trusted.
"""

from swing_screener.notify.analysis import (
    ConvictionResult,
    SignalFacts,
    analyze_conviction,
)


def _facts(ticker="AMD"):
    return SignalFacts(
        ticker=ticker, timeframe="1d", trade_type="medium", score=0.92, mtf_aligned=True,
        quality_tier="reputable", volatility_tier="high", oversold=False,
        trigger_close=100.0, atr=4.0, rsi=55.0, entry_floor=96.0, entry_ceiling=101.0,
        stop=95.0, target=110.0,
    )


_PLAYBOOK = "score=high (forward_confirmed, +0.40R, n=50)"


class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, blocks):
        self.content = blocks


class _RecordingClient:
    """Captures the create() kwargs and returns a canned response."""

    def __init__(self, resp):
        self._resp = resp
        self.kwargs = None

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **kw):
                outer.kwargs = kw
                return outer._resp

        return _M()


class _BoomClient:
    @property
    def messages(self):
        class _M:
            def create(self, **kw):
                raise RuntimeError("api down")

        return _M()


def test_conviction_nudge_up_within_bound_captures_reason_and_insight():
    resp = _Resp([_TextBlock(
        "CONVICTION: high\nREASON: strong base\nThis pick sits above the playbook edge."
    )])
    client = _RecordingClient(resp)

    out = analyze_conviction(
        _facts(), baseline="medium", playbook_text=_PLAYBOOK,
        context_text="Sentiment: bullish", chart_bytes=b"\x89PNGfake", client=client,
    )

    assert isinstance(out, ConvictionResult)
    assert out.conviction == "high"  # medium -> high is +1, within bound
    assert out.nudge_reason == "strong base"
    assert "above the playbook edge" in out.insight
    assert out.is_deep is True


def test_conviction_over_bound_jump_is_clamped_in_code():
    # Model returns "avoid" (index 0) with baseline "high" (index 3): a 3-step
    # DOWN jump. The code clamp must pull it to baseline-1 == "medium", proving
    # the bound is enforced in code and not trusted to the model.
    resp = _Resp([_TextBlock("CONVICTION: avoid\nREASON: bearish\nBig risk here.")])
    out = analyze_conviction(
        _facts(), baseline="high", playbook_text=_PLAYBOOK,
        client=_RecordingClient(resp),
    )
    assert out.conviction == "medium"  # clamped to baseline-1, NOT "avoid"
    assert out.nudge_reason == "bearish"
    assert out.is_deep is True


def test_conviction_over_bound_jump_up_is_clamped_in_code():
    # Model returns "high" (index 3) with baseline "avoid" (index 0): a 3-step UP
    # jump -> clamped to baseline+1 == "low".
    resp = _Resp([_TextBlock("CONVICTION: high\nREASON: bullish\nUpside.")])
    out = analyze_conviction(
        _facts(), baseline="avoid", playbook_text=_PLAYBOOK,
        client=_RecordingClient(resp),
    )
    assert out.conviction == "low"  # clamped to baseline+1, NOT "high"


def test_conviction_unrecognized_grade_falls_back_to_baseline():
    resp = _Resp([_TextBlock("CONVICTION: stellar\nREASON: dunno\nText.")])
    out = analyze_conviction(
        _facts(), baseline="medium", playbook_text=_PLAYBOOK,
        client=_RecordingClient(resp),
    )
    assert out.conviction == "medium"  # unrecognized grade -> baseline
    # The model still produced parseable insight prose, so this is a deep result.
    assert out.is_deep is True
    assert "Text." in out.insight


def test_conviction_falls_back_to_deterministic_on_error():
    out = analyze_conviction(
        _facts(), baseline="low", playbook_text=_PLAYBOOK, client=_BoomClient(),
    )
    assert out.conviction == "low"  # baseline preserved
    assert out.nudge_reason == "(baseline; analyst unavailable)"
    assert "ATR of 4.0% of price" in out.insight  # deterministic rationale
    assert out.is_deep is False


def test_conviction_falls_back_on_empty_reply():
    out = analyze_conviction(
        _facts(), baseline="high", playbook_text=_PLAYBOOK,
        client=_RecordingClient(_Resp([_TextBlock("")])),
    )
    assert out.conviction == "high"  # baseline preserved
    assert out.nudge_reason == "(baseline; analyst unavailable)"
    assert "ATR of 4.0% of price" in out.insight
    assert out.is_deep is False


def test_conviction_prompt_includes_baseline_playbook_and_pm1_rule():
    client = _RecordingClient(_Resp([_TextBlock("CONVICTION: medium\nREASON: agree\nok")]))
    analyze_conviction(
        _facts(), baseline="medium", playbook_text=_PLAYBOOK,
        context_text="Sentiment: mixed", chart_bytes=b"\x89PNGfake", client=client,
    )
    kw = client.kwargs
    # System prompt states the +-1 rule on the ordered scale.
    system = kw["system"]
    assert "avoid" in system and "low" in system and "medium" in system and "high" in system
    assert "one step" in system or "±1" in system or "+-1" in system

    # User content is image-first, then a text block with baseline + playbook + facts.
    content = kw["messages"][0]["content"]
    assert content[0]["type"] == "image"  # image FIRST (best practice)
    text = content[-1]["text"]
    assert "medium" in text  # the baseline
    assert _PLAYBOOK in text  # the playbook text
    assert "AMD" in text  # the facts
    assert "Sentiment: mixed" in text  # external context


def test_conviction_agree_with_baseline_keeps_baseline():
    resp = _Resp([_TextBlock("CONVICTION: medium\nREASON: agree with baseline\nSolid.")])
    out = analyze_conviction(
        _facts(), baseline="medium", playbook_text=_PLAYBOOK,
        client=_RecordingClient(resp),
    )
    assert out.conviction == "medium"
    assert out.nudge_reason == "agree with baseline"
    assert out.is_deep is True

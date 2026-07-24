"""Tests for the token-spend capture seam (Phase 6 Task 1).

The Opus call already returns ``resp.usage``; these tests prove we CAPTURE it onto
a frozen ``Usage`` and attach it to the result dataclass, compute an approximate
``est_cost_usd`` from a documented price table, and count web searches when the
usage object reports them. The capture MUST guard a missing/None ``resp.usage`` so
the analyst never crashes -- and the deterministic-fallback path leaves ``usage``
None (no model call was made).

No network: a recording fake client returns a canned response with a ``usage``
attribute; a boom client proves the None-usage fallback.
"""

import logging

import pytest

from swing_screener.notify.analysis import (
    _MODEL_PRICES,
    ConvictionResult,
    SignalAnalysis,
    SignalFacts,
    Usage,
    analyze_conviction,
    analyze_signal_deep,
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


class _ServerToolUse:
    """The usage.server_tool_use sub-object the API returns for web search."""

    def __init__(self, web_search_requests):
        self.web_search_requests = web_search_requests


class _Usage:
    def __init__(self, input_tokens, output_tokens, web_search_requests=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.server_tool_use = (
            _ServerToolUse(web_search_requests) if web_search_requests is not None else None
        )


class _Resp:
    def __init__(self, blocks, usage=None):
        self.content = blocks
        if usage is not None:
            self.usage = usage


class _RecordingClient:
    def __init__(self, resp):
        self._resp = resp

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **kw):
                return outer._resp

        return _M()


class _BoomClient:
    @property
    def messages(self):
        class _M:
            def create(self, **kw):
                raise RuntimeError("api down")

        return _M()


# ---------------------------------------------------------------------------
# Price table: documented + approximate. claude-opus-4-8 is keyed.
# ---------------------------------------------------------------------------
def test_price_table_has_opus_4_8():
    # (input_$/MTok, output_$/MTok) -- documented Anthropic list price, approximate.
    assert _MODEL_PRICES["claude-opus-4-8"] == (5.0, 25.0)


# ---------------------------------------------------------------------------
# analyze_conviction: usage captured + est_cost computed.
# ---------------------------------------------------------------------------
def test_conviction_captures_usage_tokens_and_cost():
    resp = _Resp(
        [_TextBlock("CONVICTION: medium\nREASON: agree\nok")],
        usage=_Usage(input_tokens=1000, output_tokens=500),
    )
    out = analyze_conviction(
        _facts(), baseline="medium", playbook_text=_PLAYBOOK,
        client=_RecordingClient(resp), model="claude-opus-4-8",
    )
    assert isinstance(out, ConvictionResult)
    assert out.usage is not None
    assert isinstance(out.usage, Usage)
    assert out.usage.input_tokens == 1000
    assert out.usage.output_tokens == 500
    assert out.usage.web_searches == 0  # usage had no server_tool_use
    # 1000/1e6*5 + 500/1e6*25 == 0.005 + 0.0125 == 0.0175 (+ 0 search cost)
    assert out.usage.est_cost_usd > 0
    assert abs(out.usage.est_cost_usd - 0.0175) < 1e-9


def test_conviction_counts_web_searches_and_adds_their_cost():
    resp = _Resp(
        [_TextBlock("CONVICTION: medium\nREASON: agree\nok")],
        usage=_Usage(input_tokens=1000, output_tokens=500, web_search_requests=3),
    )
    out = analyze_conviction(
        _facts(), baseline="medium", playbook_text=_PLAYBOOK,
        client=_RecordingClient(resp), model="claude-opus-4-8",
    )
    assert out.usage is not None
    assert out.usage.web_searches == 3
    # token cost 0.0175 + 3 * $0.01/search == 0.0475
    assert abs(out.usage.est_cost_usd - 0.0475) < 1e-9


def test_conviction_unknown_model_prices_at_most_expensive_known_rates():
    # FAIL-SAFE: an unpriced model must NOT collapse the token term to $0 -- the
    # spend cap would silently no-op on an unnoticed model swap, exactly the
    # failure it exists to catch. Unknown models charge the MOST EXPENSIVE known
    # rates so the cap overcounts rather than fails open.
    resp = _Resp(
        [_TextBlock("CONVICTION: medium\nREASON: agree\nok")],
        usage=_Usage(input_tokens=1000, output_tokens=500, web_search_requests=2),
    )
    out = analyze_conviction(
        _facts(), baseline="medium", playbook_text=_PLAYBOOK,
        client=_RecordingClient(resp), model="some-unpriced-model",
    )
    assert out.usage is not None
    assert out.usage.input_tokens == 1000
    # tokens at the most expensive known rates (claude-fable-5, $10/$50 per MTok):
    # 1000/1e6*10 + 500/1e6*50 == 0.01 + 0.025, + 2 web searches * $0.01 == 0.055.
    assert abs(out.usage.est_cost_usd - 0.055) < 1e-9


def test_unknown_model_warns_once_per_process_per_model(caplog):
    resp = _Resp(
        [_TextBlock("CONVICTION: medium\nREASON: agree\nok")],
        usage=_Usage(input_tokens=1000, output_tokens=500),
    )
    with caplog.at_level(logging.WARNING, logger="swing_screener.notify.analysis"):
        for _ in range(2):  # two calls, ONE warning -- once per process per model id
            analyze_conviction(
                _facts(), baseline="medium", playbook_text=_PLAYBOOK,
                client=_RecordingClient(resp), model="never-priced-model-e3a",
            )
    warnings = [r for r in caplog.records if "no price entry" in r.getMessage()]
    assert len(warnings) == 1
    assert "never-priced-model-e3a" in warnings[0].getMessage()


@pytest.mark.parametrize("model,in_price,out_price", [
    ("claude-fable-5", 10.0, 50.0),
    ("claude-sonnet-5", 3.0, 15.0),
    ("claude-opus-4-6", 5.0, 25.0),
    ("claude-haiku-4-5", 1.0, 5.0),  # E5 points the coach here -- row must exist
])
def test_new_model_ids_price_correctly(model, in_price, out_price):
    assert _MODEL_PRICES[model] == (in_price, out_price)
    resp = _Resp(
        [_TextBlock("CONVICTION: medium\nREASON: agree\nok")],
        usage=_Usage(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    out = analyze_conviction(
        _facts(), baseline="medium", playbook_text=_PLAYBOOK,
        client=_RecordingClient(resp), model=model,
    )
    assert out.usage is not None
    # 1 MTok in + 1 MTok out prices to exactly in_price + out_price dollars.
    assert abs(out.usage.est_cost_usd - (in_price + out_price)) < 1e-9


def test_conviction_missing_usage_attr_is_guarded_to_none():
    # A response with NO .usage at all (the existing fakes) must not crash and
    # must leave usage None -- the capture is best-effort.
    resp = _Resp([_TextBlock("CONVICTION: medium\nREASON: agree\nok")])  # no usage
    out = analyze_conviction(
        _facts(), baseline="medium", playbook_text=_PLAYBOOK,
        client=_RecordingClient(resp),
    )
    assert out.is_deep is True
    assert out.usage is None


def test_conviction_none_usage_is_guarded():
    resp = _Resp([_TextBlock("CONVICTION: medium\nREASON: agree\nok")], usage=None)
    # _Resp only sets .usage when not None, so this is the missing-attr case again;
    # belt-and-suspenders: explicitly set usage = None on the object.
    resp.usage = None
    out = analyze_conviction(
        _facts(), baseline="medium", playbook_text=_PLAYBOOK,
        client=_RecordingClient(resp),
    )
    assert out.usage is None


def test_conviction_fallback_path_leaves_usage_none():
    out = analyze_conviction(
        _facts(), baseline="low", playbook_text=_PLAYBOOK, client=_BoomClient(),
    )
    assert out.is_deep is False
    assert out.usage is None  # no model call was made on the fallback path


# ---------------------------------------------------------------------------
# analyze_signal_deep: same capture seam.
# ---------------------------------------------------------------------------
def test_signal_deep_captures_usage():
    resp = _Resp(
        [_TextBlock("CORE: x\nRead: y")],
        usage=_Usage(input_tokens=2000, output_tokens=1000, web_search_requests=1),
    )
    out = analyze_signal_deep(_facts(), client=_RecordingClient(resp), model="claude-opus-4-8")
    assert isinstance(out, SignalAnalysis)
    assert out.usage is not None
    assert out.usage.input_tokens == 2000
    assert out.usage.output_tokens == 1000
    assert out.usage.web_searches == 1
    # 2000/1e6*5 + 1000/1e6*25 + 1*0.01 == 0.01 + 0.025 + 0.01 == 0.045
    assert abs(out.usage.est_cost_usd - 0.045) < 1e-9


def test_signal_deep_fallback_leaves_usage_none():
    out = analyze_signal_deep(_facts(), client=_BoomClient())
    assert out.is_deep is False
    assert out.usage is None


# ---------------------------------------------------------------------------
# Billed-but-failed calls (E3b): the fallback result still carries the usage,
# so the run-level spend ceiling charges the call instead of failing open.
# ---------------------------------------------------------------------------
def test_signal_deep_billed_but_empty_reply_attaches_usage_to_fallback():
    # The API call was BILLED (usage present) but returned no usable text: the
    # deterministic fallback must still carry that usage.
    resp = _Resp(
        [_TextBlock("")],
        usage=_Usage(input_tokens=80_000, output_tokens=0, web_search_requests=2),
    )
    out = analyze_signal_deep(_facts(), client=_RecordingClient(resp), model="claude-opus-4-8")
    assert out.is_deep is False  # still the deterministic fallback
    assert "ATR of 4.0% of price" in out.rationale
    assert out.usage is not None
    assert out.usage.input_tokens == 80_000
    # 80_000/1e6*$5 + 2 searches * $0.01 == 0.40 + 0.02
    assert abs(out.usage.est_cost_usd - 0.42) < 1e-9


def test_conviction_billed_but_empty_reply_attaches_usage_to_fallback():
    resp = _Resp(
        [_TextBlock("")],
        usage=_Usage(input_tokens=80_000, output_tokens=0),
    )
    out = analyze_conviction(
        _facts(), baseline="medium", playbook_text=_PLAYBOOK,
        client=_RecordingClient(resp), model="claude-opus-4-8",
    )
    assert out.is_deep is False
    assert out.conviction == "medium"  # fallback keeps the baseline
    assert out.usage is not None
    assert abs(out.usage.est_cost_usd - 0.40) < 1e-9

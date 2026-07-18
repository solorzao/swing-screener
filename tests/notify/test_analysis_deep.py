"""Tests for the Opus deep-analysis path (chart vision + web search + thinking).

All offline: a recording fake client captures the messages.create kwargs so we
can assert the request shape (image-first, web_search tool, thinking budget)
without any network, and a boom client proves the deterministic fallback.
"""

import anthropic
import httpx

from swing_screener.notify.analysis import SignalFacts, analyze_signal_deep


def _facts(ticker="AMD"):
    return SignalFacts(
        ticker=ticker, timeframe="1d", trade_type="medium", score=0.92, mtf_aligned=True,
        quality_tier="reputable", volatility_tier="high", oversold=False,
        trigger_close=100.0, atr=4.0, rsi=55.0, entry_floor=96.0, entry_ceiling=101.0,
        stop=95.0, target=110.0,
    )


class _Citation:
    type = "web_search_result_location"

    def __init__(self, url, title):
        self.url = url
        self.title = title


class _TextBlock:
    type = "text"

    def __init__(self, text, citations=None):
        self.text = text
        self.citations = citations or []


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


def test_deep_analysis_builds_image_tool_thinking_and_parses_citations():
    resp = _Resp([_TextBlock("CORE: Strong continuation.\n\nClean setup into target.",
                             [_Citation("https://reuters.com/x", "Reuters")])])
    client = _RecordingClient(resp)

    out = analyze_signal_deep(
        _facts(), chart_bytes=b"\x89PNGfake", context_text="Fundamentals:\n- Sector: Tech",
        client=client, model="claude-opus-4-8", reasoning="high", max_searches=3,
    )

    assert out.core_reason == "Strong continuation."
    assert "Clean setup into target." in out.rationale
    assert "reuters.com/x" in out.rationale  # citation appended as a source
    assert out.is_deep is True  # marks the output as the genuine deep path

    kw = client.kwargs
    assert kw["model"] == "claude-opus-4-8"
    # opus-4.8 reasoning API: adaptive thinking + output_config.effort (NOT budget_tokens)
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "high"}
    assert kw["max_tokens"] == 16000
    tool = kw["tools"][0]
    # 20260209 = dynamic filtering: search-result tokens are pruned BEFORE billing
    assert tool["type"] == "web_search_20260209"
    assert tool["name"] == "web_search" and tool["max_uses"] == 3
    content = kw["messages"][0]["content"]
    assert content[0]["type"] == "image"  # image FIRST (best practice)
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1]["type"] == "text" and "AMD" in content[1]["text"]


def test_deep_analysis_reasoning_none_omits_thinking():
    client = _RecordingClient(_Resp([_TextBlock("CORE: ok\n\nbody")]))
    analyze_signal_deep(_facts(), client=client, reasoning="none")
    assert "thinking" not in client.kwargs and "output_config" not in client.kwargs
    assert client.kwargs["max_tokens"] == 2000


def test_deep_analysis_without_chart_sends_no_image_block():
    client = _RecordingClient(_Resp([_TextBlock("CORE: ok\n\nbody")]))
    analyze_signal_deep(_facts(), chart_bytes=None, client=client)
    content = client.kwargs["messages"][0]["content"]
    assert all(b["type"] != "image" for b in content)


def test_deep_analysis_web_search_disabled_omits_tools():
    client = _RecordingClient(_Resp([_TextBlock("CORE: ok\n\nbody")]))
    analyze_signal_deep(_facts(), client=client, web_search=False)
    assert "tools" not in client.kwargs


def test_deep_analysis_falls_back_to_deterministic_on_error():
    out = analyze_signal_deep(_facts(), client=_BoomClient())
    assert out.core_reason  # deterministic core, non-empty
    assert "ATR of 4.0% of price" in out.rationale  # deterministic rationale (percent ATR)
    assert out.is_deep is False  # a fallback is NOT labelled deep


def test_deep_analysis_falls_back_on_empty_reply():
    out = analyze_signal_deep(_facts(), client=_RecordingClient(_Resp([_TextBlock("")])))
    assert "ATR of 4.0% of price" in out.rationale  # empty model text -> fallback


def _bad_request(message):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(message, response=httpx.Response(400, request=req), body=None)


class _RetryClient:
    """Rejects the reasoning params on the first call, succeeds on the retry."""

    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **kw):
                outer.calls.append(kw)
                if len(outer.calls) == 1:
                    raise _bad_request('"thinking.type.enabled" is not supported for this model')
                return outer._resp

        return _M()


def test_deep_analysis_retries_without_reasoning_on_model_rejection():
    # A model that rejects adaptive thinking / output_config should NOT drop to the
    # deterministic narrator -- we retry once WITHOUT those params and still get a
    # real model analysis.
    client = _RetryClient(_Resp([_TextBlock("CORE: ok\n\nreal model body")]))
    out = analyze_signal_deep(_facts(), client=client, reasoning="high")

    assert "real model body" in out.rationale  # succeeded on the retry, not a fallback
    assert len(client.calls) == 2
    assert "thinking" in client.calls[0] and "output_config" in client.calls[0]
    assert "thinking" not in client.calls[1] and "output_config" not in client.calls[1]


class _LegacyToolClient:
    """Rejects the web_search_20260209 tool type, succeeds once it's downgraded."""

    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **kw):
                outer.calls.append(kw)
                if any(t.get("type") == "web_search_20260209" for t in kw.get("tools", [])):
                    raise _bad_request(
                        "tools.0: `web_search_20260209` is not supported for this model"
                    )
                return outer._resp

        return _M()


def test_deep_analysis_retries_with_legacy_web_search_on_tool_rejection():
    # An older SWING_ANALYSIS_MODEL that rejects the 20260209 tool must NOT drop to
    # the deterministic narrator -- we retry with the legacy 20250305 tool type and
    # still get a real model analysis.
    client = _LegacyToolClient(_Resp([_TextBlock("CORE: ok\n\nreal model body")]))
    out = analyze_signal_deep(_facts(), client=client)

    assert "real model body" in out.rationale  # succeeded on the retry, not a fallback
    assert out.is_deep is True
    assert len(client.calls) == 2
    assert client.calls[0]["tools"][0]["type"] == "web_search_20260209"
    assert client.calls[1]["tools"][0]["type"] == "web_search_20250305"
    assert client.calls[1]["tools"][0]["max_uses"] == 4  # rest of the tool config kept


class _DoubleRejectClient:
    """An old model that rejects BOTH the reasoning shape and the 20260209 tool:
    call 1 trips the thinking degrade, call 2 trips the web-search degrade, call 3
    succeeds -- one API call may need both retries in sequence."""

    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **kw):
                outer.calls.append(kw)
                if "thinking" in kw:
                    raise _bad_request('"thinking.type.enabled" is not supported for this model')
                if any(t.get("type") == "web_search_20260209" for t in kw.get("tools", [])):
                    raise _bad_request(
                        "tools.0: `web_search_20260209` is not supported for this model"
                    )
                return outer._resp

        return _M()


def test_deep_analysis_survives_both_thinking_and_tool_rejection():
    client = _DoubleRejectClient(_Resp([_TextBlock("CORE: ok\n\nreal model body")]))
    out = analyze_signal_deep(_facts(), client=client, reasoning="high")

    assert "real model body" in out.rationale  # both degrades applied, still a real analysis
    assert len(client.calls) == 3
    assert "thinking" not in client.calls[2]
    assert client.calls[2]["tools"][0]["type"] == "web_search_20250305"

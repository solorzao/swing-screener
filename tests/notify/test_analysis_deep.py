"""Tests for the Opus deep-analysis path (chart vision + web search + thinking).

All offline: a recording fake client captures the messages.create kwargs so we
can assert the request shape (image-first, web_search tool, thinking budget)
without any network, and a boom client proves the deterministic fallback.
"""

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

    kw = client.kwargs
    assert kw["model"] == "claude-opus-4-8"
    assert kw["thinking"] == {"type": "enabled", "budget_tokens": 12000}
    assert kw["max_tokens"] > 12000  # answer room on top of the thinking budget
    tool = kw["tools"][0]
    assert tool["type"] == "web_search_20250305"
    assert tool["name"] == "web_search" and tool["max_uses"] == 3
    content = kw["messages"][0]["content"]
    assert content[0]["type"] == "image"  # image FIRST (best practice)
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1]["type"] == "text" and "AMD" in content[1]["text"]


def test_deep_analysis_reasoning_none_omits_thinking():
    client = _RecordingClient(_Resp([_TextBlock("CORE: ok\n\nbody")]))
    analyze_signal_deep(_facts(), client=client, reasoning="none")
    assert "thinking" not in client.kwargs
    assert client.kwargs["max_tokens"] == 1500  # just the answer allowance


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


def test_deep_analysis_falls_back_on_empty_reply():
    out = analyze_signal_deep(_facts(), client=_RecordingClient(_Resp([_TextBlock("")])))
    assert "ATR of 4.0% of price" in out.rationale  # empty model text -> fallback

"""Tests for analyze_ticker_deep -- the combined Opus multi-timeframe analyst.

Offline: a fake client returns a canned reply (or raises) so no network is hit.
We assert the parsed CORE summary + is_deep flag on success, the request shape
(chart images FIRST, web_search tool, thinking effort), and the deterministic
multi-timeframe fallback on ANY failure.
"""

from datetime import datetime

from swing_screener.notify.analysis import analyze_ticker_deep
from swing_screener.notify.ticker_report import TickerReport, TimeframeRead


class _FakeResp:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text, "citations": []})()]


class _FakeClient:
    def __init__(self, text=None, exc=None):
        self._text, self._exc = text, exc
        self.kwargs = None

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.kwargs = kw
        if self._exc:
            raise self._exc
        return _FakeResp(self._text)


def _report(reads=None):
    return TickerReport(
        ticker="AAPL",
        name="Apple Inc",
        run_at=datetime(2026, 6, 16, 12, 0, 0),
        reads=reads if reads is not None else [
            TimeframeRead(
                timeframe="1d", ha_trend="bullish", ema_aligned=True,
                rsi=58.0, atr_pct=0.023,
            ),
            TimeframeRead(
                timeframe="1wk", ha_trend="bearish", ema_aligned=False,
                rsi=44.0, atr_pct=0.041,
            ),
        ],
    )


_CANNED = (
    "CORE: Constructive across timeframes\n"
    "4h: up\n1d: up\n1wk: flat\n1mo: up\n"
    "Setups: 1d firing\nRisk: macro\nWatch: 100"
)


def test_analyze_ticker_deep_parses_core_and_marks_deep():
    client = _FakeClient(text=_CANNED)
    summary, analysis_text, is_deep = analyze_ticker_deep(_report(), client=client)

    assert summary == "Constructive across timeframes"
    assert "1d:" in analysis_text
    assert is_deep is True


def test_analyze_ticker_deep_builds_images_first_then_tf_text():
    client = _FakeClient(text=_CANNED)
    analyze_ticker_deep(
        _report(), charts=[b"\x89PNGone", b"\x89PNGtwo"], client=client,
        reasoning="high", max_searches=3,
    )
    kw = client.kwargs
    assert kw["model"] == "claude-opus-4-8"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "high"}
    assert kw["max_tokens"] == 16000
    tool = kw["tools"][0]
    # 20260209 = dynamic filtering: search-result tokens are pruned BEFORE billing
    assert tool["type"] == "web_search_20260209"
    assert tool["name"] == "web_search" and tool["max_uses"] == 3
    content = kw["messages"][0]["content"]
    # both charts come FIRST as image blocks, then a single text block
    assert content[0]["type"] == "image" and content[1]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[2]["type"] == "text"
    text = content[2]["text"]
    assert "AAPL" in text and "1d" in text and "1wk" in text


def test_analyze_ticker_deep_web_search_disabled_omits_tools():
    client = _FakeClient(text=_CANNED)
    analyze_ticker_deep(_report(), client=client, web_search=False)
    assert "tools" not in client.kwargs


def test_analyze_ticker_deep_falls_back_on_error():
    summary, analysis_text, is_deep = analyze_ticker_deep(
        _report(), client=_FakeClient(exc=RuntimeError("boom"))
    )
    assert is_deep is False
    assert summary  # non-empty deterministic summary
    assert "AAPL" in summary
    # one line per read in the deterministic fallback text
    assert "1d:" in analysis_text and "1wk:" in analysis_text
    assert "RSI" in analysis_text


def test_analyze_ticker_deep_falls_back_on_empty_reply():
    summary, analysis_text, is_deep = analyze_ticker_deep(_report(), client=_FakeClient(text=""))
    assert is_deep is False
    assert "1d:" in analysis_text

"""analyze_lab_deep -- the TICKER LAB Opus analyst. Offline via the fake-client
seam. Asserts: the request shape at MAX effort (adaptive thinking +
output_config effort 'max', the raised max_tokens cap, the web_search tool, the
facts in the user turn), usage capture on the uncapped path, the
deterministic-facts fallback on ANY failure, and that the extended effort map
never silently disables thinking for 'xhigh'/'max' (the pre-fix behavior)."""

from swing_screener.notify.analysis import (
    _REASONING_EFFORT,
    _REASONING_MAX_TOKENS,
    EmptyAnalysisError,
    LabDeepAnalysis,
    analyze_lab_deep,
)


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.server_tool_use = None


class _FakeResp:
    def __init__(self, text: str, usage: _FakeUsage | None = None) -> None:
        self.content = [type("B", (), {"type": "text", "text": text, "citations": []})()]
        if usage is not None:
            self.usage = usage


class _FakeClient:
    def __init__(self, text: str | None = None, exc: Exception | None = None,
                 usage: _FakeUsage | None = None) -> None:
        self._text, self._exc, self._usage = text, exc, usage
        self.kwargs: dict | None = None
        self.with_options_kwargs: dict | None = None

    def with_options(self, **kw):  # the >16k-max_tokens timeout override path
        self.with_options_kwargs = kw
        return self

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.kwargs = kw
        if self._exc is not None:
            raise self._exc
        assert self._text is not None
        return _FakeResp(self._text, usage=self._usage)


_FACTS = "== 1d (daily) -- 260 bars ==\nlast close: 100.00\nresistance: 105.00"
_NOTE = "## Read\nConstructive.\n\n## Levels that matter\n- 105.00"


def test_effort_map_covers_xhigh_and_max() -> None:
    """An unknown key silently DISABLES thinking (_analyst_call omits the
    params), so 'max' must be a real member of both maps."""
    assert _REASONING_EFFORT["max"] == "max"
    assert _REASONING_EFFORT["xhigh"] == "xhigh"
    assert _REASONING_MAX_TOKENS["max"] > _REASONING_MAX_TOKENS["high"]


def test_lab_deep_request_shape_at_max_effort() -> None:
    client = _FakeClient(text=_NOTE, usage=_FakeUsage(1000, 2000))
    out = analyze_lab_deep("NVDA", _FACTS, client=client, reasoning="max")

    assert isinstance(out, LabDeepAnalysis)
    assert out.is_deep is True
    assert out.report.startswith("## Read")
    kw = client.kwargs
    assert kw is not None
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "max"}
    assert kw["max_tokens"] == _REASONING_MAX_TOKENS["max"]
    assert kw["tools"][0]["name"] == "web_search"
    user_text = kw["messages"][0]["content"][0]["text"]
    assert "NVDA" in user_text and "resistance: 105.00" in user_text
    assert "ground truth" in user_text
    # >16k max_tokens rides an explicit timeout (the SDK's non-streaming guard).
    assert client.with_options_kwargs is not None
    assert "timeout" in client.with_options_kwargs


def test_lab_deep_usage_captured() -> None:
    client = _FakeClient(text=_NOTE, usage=_FakeUsage(1000, 2000))
    out = analyze_lab_deep("NVDA", _FACTS, client=client, reasoning="max")
    assert out.usage is not None
    assert out.usage.input_tokens == 1000 and out.usage.output_tokens == 2000
    assert out.usage.est_cost_usd > 0


def test_lab_deep_failure_serves_facts_verbatim() -> None:
    client = _FakeClient(exc=RuntimeError("api down"))
    out = analyze_lab_deep("NVDA", _FACTS, client=client, reasoning="max")
    assert out.is_deep is False
    assert _FACTS in out.report          # the deterministic study, verbatim
    assert out.usage is None             # no billed call happened


def test_lab_deep_empty_reply_still_carries_billed_usage() -> None:
    client = _FakeClient(text="   ", usage=_FakeUsage(500, 0))
    out = analyze_lab_deep("NVDA", _FACTS, client=client, reasoning="max")
    assert out.is_deep is False
    assert out.usage is not None and out.usage.input_tokens == 500


def test_empty_analysis_error_type_still_exported() -> None:
    # analyze_lab_deep leans on the same EmptyAnalysisError carry path as the
    # other analysts; pin the class so a rename breaks loudly here too.
    assert issubclass(EmptyAnalysisError, ValueError)

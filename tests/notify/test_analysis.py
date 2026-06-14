from swing_screener.notify.analysis import SignalFacts, analyze_signal


def _facts(ticker="AMD"):
    return SignalFacts(
        ticker=ticker, timeframe="1d", trade_type="medium", score=0.92, mtf_aligned=True,
        quality_tier="reputable", volatility_tier="high", oversold=False,
        trigger_close=100.0, atr=4.0, rsi=55.0, entry_floor=96.0, entry_ceiling=101.0,
        stop=95.0, target=110.0,
    )


class _Block:
    type = "text"
    def __init__(self, text): self.text = text


class _Resp:
    def __init__(self, text): self.content = [_Block(text)]


class _FakeClient:
    def __init__(self, text): self._text = text
    @property
    def messages(self):
        outer = self
        class _M:
            def create(self, **kw): return _Resp(outer._text)
        return _M()


class _BoomClient:
    @property
    def messages(self):
        class _M:
            def create(self, **kw): raise RuntimeError("api down")
        return _M()


def test_parses_core_and_rationale_from_model():
    client = _FakeClient("CORE: AMD daily continuation looks strong.\n\nAMD is in a daily uptrend; "
                         "entry 96-101, stop 95, target 110.")
    out = analyze_signal(_facts(), client=client)
    assert out.core_reason == "AMD daily continuation looks strong."
    assert "AMD" in out.rationale and "110" in out.rationale


def test_graceful_fallback_when_api_fails():
    out = analyze_signal(_facts(), client=_BoomClient())
    assert out.core_reason  # non-empty deterministic fallback
    assert "AMD" in out.rationale
    assert "95" in out.rationale and "110" in out.rationale  # mentions stop + target


def test_risk_reward_property():
    assert _facts().risk_reward == (110.0 - 101.0) / (101.0 - 95.0)

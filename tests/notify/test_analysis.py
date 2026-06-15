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


def test_atr_pct_property():
    # ATR is stored in dollars; atr_pct normalizes it to a fraction of price so
    # the signal is comparable across high- and low-priced names.
    assert _facts().atr_pct == 4.0 / 100.0


def test_atr_pct_property_guards_zero_price():
    facts = SignalFacts(
        ticker="X", timeframe="1d", trade_type="medium", score=0.5, mtf_aligned=False,
        quality_tier="mid", volatility_tier="low", oversold=False,
        trigger_close=0.0, atr=4.0, rsi=50.0, entry_floor=1.0, entry_ceiling=2.0,
        stop=0.5, target=3.0,
    )
    assert facts.atr_pct == 0.0  # no ZeroDivisionError on a degenerate price


def test_deterministic_rationale_shows_atr_as_percent_not_dollars():
    # atr=4.0 on trigger_close=100.0 -> 4.0% of price. The narration must express
    # ATR as a percentage, never the bare dollar figure.
    out = analyze_signal(_facts(), client=_BoomClient())  # forces deterministic path
    assert "ATR of 4.0% of price" in out.rationale  # percentage, not the raw dollar ATR

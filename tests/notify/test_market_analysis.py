from dataclasses import replace
from datetime import date

from swing_screener.notify.market_analysis import (
    analyze_market_deep,
    facts_block,
)
from swing_screener.pipeline.market import MarketFacts, TimeframeHA


def _facts(*, alignment="mixed", vix_spike=True, inverted=True):
    return MarketFacts(
        as_of=date(2026, 6, 26), spy_close=540.0,
        ha={"1mo": TimeframeHA("1mo", "bull", False, 6),
            "1wk": TimeframeHA("1wk", "bear", True, 1),
            "1d": TimeframeHA("1d", "bear", True, 3)},
        ha_alignment=alignment, ha_alignment_note="Timeframes DIVERGE (1mo bull, 1wk bear, 1d bear).",
        spy_vs_200dma="above", vol_bucket="elevated",
        vix=28.0, vix_rank=85.0, vix_spike=vix_spike,
        ten_year=4.2, three_month=4.6, yield_inverted=inverted, bond_trend="bear",
    )


class _Block:
    type = "text"
    def __init__(self, text):
        self.text = text
        self.citations = []


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = None


class _FakeClient:
    def __init__(self, text):
        self._text = text

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **kw):
                return _Resp(outer._text)
        return _M()


class _BoomClient:
    @property
    def messages(self):
        class _M:
            def create(self, **kw):
                raise RuntimeError("api down")
        return _M()


def test_facts_block_surfaces_the_key_signals():
    block = facts_block(_facts())
    assert "alignment: mixed" in block
    assert "FRESH FLIP" in block          # weekly + daily flipped
    assert "PANIC SPIKE" in block         # vix_spike
    assert "INVERTED" in block            # 3m > 10y


def test_facts_block_renders_v2_signals():
    f = replace(_facts(), recession_prob=42.0, vix_term_ratio=1.10, vix_backwardation=True,
                credit_pctile=12.0, credit_chg_4w=-2.0, cyc_def_trend="bear", cyc_def_chg_4w=-3.0,
                breadth_trend="bear", breadth_chg_4w=-1.5)
    block = facts_block(f)
    assert "Recession probability" in block and "42%" in block
    assert "BACKWARDATION" in block
    assert "spreads WIDENING" in block
    assert "Rotation cyclicals vs defensives" in block
    assert "Breadth participation" in block


def test_analyze_market_deep_parses_labelled_report():
    reply = ("CORE: Transitional tape -- daily rolling over under a bullish monthly.\n"
             "Regime: weekly+daily HA flipped bear vs a bull monthly.\n"
             "Risk: a weekly close that confirms the flip.")
    out = analyze_market_deep(_facts(), client=_FakeClient(reply), web_search=False)
    assert out.is_deep is True
    assert out.core.startswith("Transitional tape")
    assert "Regime:" in out.report


def test_analyze_market_deep_falls_back_deterministically():
    out = analyze_market_deep(_facts(alignment="aligned_bear"), client=_BoomClient(), web_search=False)
    assert out.is_deep is False
    assert "Risk-off" in out.core            # stance from the alignment
    assert "Deterministic fallback" in out.report
    assert "alignment: aligned_bear" in out.report

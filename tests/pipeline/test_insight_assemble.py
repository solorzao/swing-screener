"""Assembly + persistence layer of the insight engine (``pipeline.insight``).

``build_order_intent`` stitches the DETERMINISTIC levels (copied verbatim from the
facts -- never recomputed) together with the analyst's FINAL conviction + insight
and the conviction-scaled R-based size. ``record_analyst_call`` persists each call
(both convictions + the nudge reason + model) UNSCORED, for the calibration loop.
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db.models import AnalystCall
from swing_screener.db.session import get_engine
from swing_screener.notify.analysis import ConvictionResult, SignalFacts, Usage
from swing_screener.pipeline.insight import (
    OrderIntent,
    build_order_intent,
    record_analyst_call,
    size_order,
)


def _facts(**over: object) -> SignalFacts:
    base: dict[str, object] = dict(
        ticker="AMD", timeframe="1d", trade_type="medium", score=0.85,
        mtf_aligned=True, quality_tier="high", volatility_tier="med", oversold=False,
        trigger_close=100.0, atr=2.0, rsi=55.0,
        entry_floor=99.0, entry_ceiling=101.0, stop=95.0, target=110.0,
    )
    base.update(over)
    return SignalFacts(**base)  # type: ignore[arg-type]


def _conv(**over: object) -> ConvictionResult:
    base: dict[str, object] = dict(
        conviction="high", nudge_reason="sector momentum confirms",
        insight="Strong continuation; key risk is earnings next week.", is_deep=True,
    )
    base.update(over)
    return ConvictionResult(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# build_order_intent: levels copied verbatim; final conviction + sized.
# ---------------------------------------------------------------------------
def test_build_order_intent_copies_levels_verbatim() -> None:
    facts = _facts()
    intent = build_order_intent(
        facts, _conv(), play_type="continuation", edge_played="score=0.80-1.00",
        risk_unit_dollars=250.0,
    )
    assert isinstance(intent, OrderIntent)
    # the deterministic levels are copied from facts, NEVER recomputed.
    assert intent.entry_floor == facts.entry_floor == 99.0
    assert intent.entry_ceiling == facts.entry_ceiling == 101.0
    assert intent.stop == facts.stop == 95.0
    assert intent.target == facts.target == 110.0
    assert intent.ticker == "AMD"
    assert intent.timeframe == "1d"
    assert intent.play_type == "continuation"
    assert intent.edge_played == "score=0.80-1.00"


def test_build_order_intent_uses_final_conviction_and_insight() -> None:
    facts = _facts()
    conv = _conv(conviction="medium", insight="Mixed picture.")
    intent = build_order_intent(
        facts, conv, play_type="continuation", edge_played="e", risk_unit_dollars=250.0,
    )
    assert intent.conviction == "medium"  # the FINAL (clamped) conviction
    assert intent.insight == "Mixed picture."


def test_build_order_intent_sizes_from_size_order() -> None:
    facts = _facts(entry_ceiling=12.0, stop=10.0)
    conv = _conv(conviction="high")
    intent = build_order_intent(
        facts, conv, play_type="continuation", edge_played="e",
        risk_unit_dollars=250.0, max_shares=50,
    )
    shares, risk = size_order(
        conviction="high", entry_ceiling=12.0, stop=10.0,
        risk_unit_dollars=250.0, max_shares=50,
    )
    assert intent.shares == shares == 50
    assert intent.risk_dollars == risk == 100.0


def test_build_order_intent_unconfigured_risk_unit_zero_size() -> None:
    intent = build_order_intent(
        _facts(), _conv(), play_type="continuation", edge_played="e",
        risk_unit_dollars=0.0,
    )
    assert intent.shares == 0
    assert intent.risk_dollars == 0.0


def test_build_order_intent_order_spec_fields() -> None:
    # the order-spec fields: long side, limit price COPIED from the facts ceiling
    # (the deterministic buy-at-or-below level -- never computed), market/day defaults.
    facts = _facts()
    intent = build_order_intent(
        facts, _conv(), play_type="continuation", edge_played="e",
        risk_unit_dollars=250.0,
    )
    assert intent.side == "long"
    assert intent.limit_price == facts.entry_ceiling == 101.0
    assert intent.order_type == "market"
    assert intent.time_in_force == "day"


# ---------------------------------------------------------------------------
# record_analyst_call: persists both convictions + nudge + model, UNSCORED.
# ---------------------------------------------------------------------------
def test_record_analyst_call_persists_unscored() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        call = record_analyst_call(
            s, facts=_facts(), run_date=date(2026, 6, 19),
            created_date=date(2026, 6, 20), baseline_conviction="medium",
            conviction_result=_conv(conviction="high",
                                    nudge_reason="sector momentum confirms"),
            model="claude-opus-4-8", play_type="continuation",
        )
        assert call.id is not None
        row = s.query(AnalystCall).one()
        assert row.ticker == "AMD"
        assert row.timeframe == "1d"
        assert row.play_type == "continuation"
        assert row.run_date == date(2026, 6, 19)
        assert row.created_date == date(2026, 6, 20)
        assert row.baseline_conviction == "medium"
        assert row.final_conviction == "high"
        assert row.nudge_reason == "sector momentum confirms"
        assert row.model == "claude-opus-4-8"
        # persisted UNSCORED -- the calibration loop fills these in later.
        assert row.realized_r is None
        assert row.scored_at is None
        # no usage on this conviction result -> cost columns NULL.
        assert row.input_tokens is None
        assert row.output_tokens is None
        assert row.web_searches is None
        assert row.est_cost_usd is None


def test_record_analyst_call_persists_token_spend() -> None:
    # When the conviction result carries Usage, the four cost columns are written.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        record_analyst_call(
            s, facts=_facts(), run_date=date(2026, 6, 19),
            created_date=date(2026, 6, 20), baseline_conviction="medium",
            conviction_result=_conv(
                conviction="high",
                usage=Usage(input_tokens=1000, output_tokens=500, web_searches=2,
                            est_cost_usd=0.0375),
            ),
            model="claude-opus-4-8", play_type="continuation",
        )
        row = s.query(AnalystCall).one()
        assert row.input_tokens == 1000
        assert row.output_tokens == 500
        assert row.web_searches == 2
        assert row.est_cost_usd == 0.0375


def test_record_analyst_call_fallback_usage_none_leaves_cost_null() -> None:
    # The deterministic-fallback ConvictionResult has usage=None -> all NULL.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        record_analyst_call(
            s, facts=_facts(), run_date=date(2026, 6, 19),
            created_date=date(2026, 6, 20), baseline_conviction="low",
            conviction_result=_conv(conviction="low", is_deep=False, usage=None),
            model="claude-opus-4-8", play_type="continuation",
        )
        row = s.query(AnalystCall).one()
        assert row.input_tokens is None
        assert row.output_tokens is None
        assert row.web_searches is None
        assert row.est_cost_usd is None

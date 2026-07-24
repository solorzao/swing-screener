"""Coach deterministic trade-review grader: code owns every number; no MAE/MFE
(the Trade table has no water marks); robinhood is premium-$, no R, no A+ grade."""

from datetime import UTC, date, datetime

from swing_screener.db.models import OptionPaperTrade, Trade
from swing_screener.journal.coach_grade import (
    TradeReviewFacts,
    equity_review_facts,
    facts_dict,
    option_review_facts,
)


def _trade(*, entry=100.0, stop=95.0, target=110.0, exit_price=None,
           exit_reason=None, entry_date=date(2026, 7, 1), exit_date=None,
           override=None, emotional_state=None):
    return Trade(
        ticker="AMD", timeframe="1d", horizon="medium", entry_date=entry_date,
        entry_price=entry, size=1.0, stop=stop, target=target,
        status="closed" if exit_price is not None else "open",
        exit_price=exit_price, exit_date=exit_date, exit_reason=exit_reason,
        override=override, emotional_state=emotional_state,
    )


def test_equity_facts_target_win():
    t = _trade(exit_price=110.0, exit_reason="target", exit_date=date(2026, 7, 5))
    f = equity_review_facts(t)
    assert f.book == "manual_equity"
    assert f.unit == "R"
    assert f.result == 2.0                 # (110-100)/(100-95)
    assert f.outcome == "target"
    assert f.hold_days == 4
    assert f.moved_stop is False
    assert f.mae_r is None and f.mfe_r is None   # not computable -> honest None


def test_equity_facts_moved_stop_override_is_flagged():
    t = _trade(exit_price=96.0, exit_reason="stop", exit_date=date(2026, 7, 2),
               override="stop moved +1.1%", emotional_state="fomo")
    f = equity_review_facts(t)
    assert f.outcome == "stop"
    assert f.moved_stop is True
    assert f.override == "stop moved +1.1%"
    assert f.emotional_state == "fomo"


def test_equity_result_none_when_open_or_bad_risk():
    assert equity_review_facts(_trade(exit_price=None)).result is None       # open
    bad = _trade(entry=100.0, stop=100.0, exit_price=105.0, exit_date=date(2026, 7, 2))
    assert equity_review_facts(bad).result is None                           # risk <= 0


def test_option_facts_are_premium_dollars_never_r():
    o = OptionPaperTrade(
        account="robinhood", strategy="gex", underlying="SPY", direction="long",
        opened_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC), closed_at=datetime(2026, 7, 3, 15, 0, tzinfo=UTC),
        premium_pnl=42.0, status="closed", exit_reason="manual",
    )
    f = option_review_facts(o)
    assert f.book == "robinhood"
    assert f.unit == "$"
    assert f.result == 42.0
    assert f.hold_days == 2
    assert f.mae_r is None and f.mfe_r is None
    # premium book never claims an R or an A+ grade
    d = facts_dict(f)
    assert "a_plus_grade" not in d and d["unit"] == "$"


def test_facts_dict_is_json_shaped():
    f = equity_review_facts(_trade(exit_price=110.0, exit_reason="target",
                                   exit_date=date(2026, 7, 5)))
    d = facts_dict(f)
    assert d["result"] == 2.0 and d["outcome"] == "target"
    assert isinstance(d, dict) and "book" in d
    assert isinstance(f, TradeReviewFacts)

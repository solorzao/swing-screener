"""Auto-tagger: deterministic proposals only. Nothing here writes journal_trade_tags
-- proposals are parked in the review's facts_json and only applied on human confirm
(a written mistake tag is a live confession the instant it exists)."""

from swing_screener.journal.auto_tag import TagProposal, propose_tags
from swing_screener.journal.coach_grade import TradeReviewFacts


def _facts(**kw) -> TradeReviewFacts:
    base = {
        "book": "manual_equity", "symbol": "AMD", "unit": "R", "result": 2.0, "outcome": "target",
        "hold_days": 3, "moved_stop": False, "override": None, "emotional_state": None,
        "exit_reason": "target",
    }
    base.update(kw)
    return TradeReviewFacts(**base)  # type: ignore[arg-type]


def _names(props):
    return {p.name for p in props}


def test_clean_target_win_proposes_nothing():
    assert propose_tags(_facts()) == []


def test_moved_stop_proposes_moved_stop_mistake():
    props = propose_tags(_facts(moved_stop=True, override="stop moved +1.1%",
                                outcome="stop", result=-0.8))
    assert "moved_stop" in _names(props)
    assert all(isinstance(p, TagProposal) for p in props)
    moved = next(p for p in props if p.name == "moved_stop")
    assert moved.kind == "mistake" and moved.reason


def test_manual_close_in_profit_proposes_early_exit():
    props = propose_tags(_facts(outcome="other", exit_reason="manual", result=0.6))
    assert "early_exit" in _names(props)


def test_emotion_is_context_not_mistake():
    props = propose_tags(_facts(emotional_state="fomo"))
    fomo = next(p for p in props if p.name == "fomo")
    assert fomo.kind == "context"          # emotion is context, never auto-confessed as a mistake


def test_override_without_stop_move_proposes_deviation_context():
    props = propose_tags(_facts(override="entry +0.50R above ceiling"))
    dev = next(p for p in props if p.name == "deviation")
    assert dev.kind == "context"

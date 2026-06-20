from swing_screener.analytics.performance import summarize
from swing_screener.config import StrategyConfig
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.optimize import OptimizeResult
from swing_screener.pipeline.propose import Proposal, apply_to_config, propose

BASE = StrategyConfig(max_extension_atr=2.0)   # incumbent gate -> grid name "ext_2.0"


def _summary(rs):
    trades = [PaperTrade(ticker="X", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
                         fill_status="filled", stop=95.0, target=110.0, risk=5.0,
                         status="closed", realized_r=r, hold_bars=3) for r in rs]
    return summarize(trades)


def _result(*, winner, in_sample, out_of_sample):
    return OptimizeResult(in_sample=in_sample, out_of_sample=out_of_sample, winner=winner)


def test_proposes_when_winner_beats_incumbent_out_of_sample():
    # ext_1.5 wins in-sample AND holds out-of-sample on a trusted, positive sample that
    # beats the incumbent (ext_2.0) -> a proposal is made.
    result = _result(
        winner="ext_1.5",
        in_sample={"ext_1.5": _summary([1.0] * 30), "ext_2.0": _summary([0.5] * 30)},
        out_of_sample={"ext_1.5": _summary([0.8] * 25), "ext_2.0": _summary([0.3] * 25)},
    )
    p = propose(result, BASE, min_oos_trades=20)
    assert isinstance(p, Proposal)
    assert p.knob == "max_extension_atr" and p.current == 2.0 and p.proposed == 1.5
    assert "2.0 → 1.5" in p.title


def test_no_proposal_when_winner_is_the_incumbent():
    result = _result(
        winner="ext_2.0",
        in_sample={"ext_2.0": _summary([1.0] * 30)},
        out_of_sample={"ext_2.0": _summary([1.0] * 30)},
    )
    assert propose(result, BASE) is None


def test_no_proposal_when_out_of_sample_sample_is_thin():
    result = _result(
        winner="ext_1.5",
        in_sample={"ext_1.5": _summary([1.0] * 30)},
        out_of_sample={"ext_1.5": _summary([2.0] * 3)},   # only 3 OOS trades -> untrusted
    )
    assert propose(result, BASE, min_oos_trades=20) is None


def test_no_proposal_when_edge_is_in_sample_only():
    # great in-sample, but out-of-sample lower bound <= 0 (noise) -> overfit, propose nothing
    result = _result(
        winner="ext_1.5",
        in_sample={"ext_1.5": _summary([2.0] * 30)},
        out_of_sample={"ext_1.5": _summary([3.0, -3.0] * 12)},  # mean ~0, ci_low < 0
    )
    assert propose(result, BASE, min_oos_trades=20) is None


def test_no_proposal_when_winner_does_not_beat_incumbent_oos():
    result = _result(
        winner="ext_1.5",
        in_sample={"ext_1.5": _summary([1.0] * 30), "ext_2.0": _summary([0.9] * 30)},
        out_of_sample={"ext_1.5": _summary([0.5] * 25), "ext_2.0": _summary([0.9] * 25)},
    )  # incumbent actually does better OOS
    assert propose(result, BASE, min_oos_trades=20) is None


def test_no_proposal_when_no_winner():
    assert propose(_result(winner=None, in_sample={}, out_of_sample={}), BASE) is None


def test_apply_to_config_edits_the_gate_default():
    src = (
        "    stop_buffer_atr: float = 0.25\n"
        "    max_extension_atr: float = 2.0\n"
        "    digest_repeat_cooldown_days: int | None = 1\n"
    )
    p = Proposal(knob="max_extension_atr", current=2.0, proposed=1.5, title="t", body="b")
    out = apply_to_config(src, p)
    assert "max_extension_atr: float = 1.5" in out
    assert "stop_buffer_atr: float = 0.25" in out          # only the gate line changed
    assert "max_extension_atr: float = 2.0" not in out


def test_apply_to_config_raises_if_field_missing():
    p = Proposal(knob="max_extension_atr", current=2.0, proposed=1.5, title="t", body="b")
    import pytest
    with pytest.raises(ValueError, match="not found"):
        apply_to_config("nothing here\n", p)

from swing_screener.analytics.performance import _CLUSTER_FLOOR, summarize
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


def _trades(by_ticker_rs, variant):
    """Closed-filled PaperTrades for ``{ticker: [R, ...]}`` tagged with ``variant``.

    Distinct ``.ticker`` per key (so the clustered bootstrap sees real clusters) and a distinct
    ``.variant`` per book (so winner and incumbent books don't bleed into one another).
    """
    return [PaperTrade(ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
                       fill_status="filled", stop=95.0, target=110.0, risk=5.0, variant=variant,
                       status="closed", realized_r=r, hold_bars=3)
            for ticker, rs in by_ticker_rs.items() for r in rs]


def _summary_of(trades):
    """Summarize a winner/incumbent trade book so its summary line and the trade-level book
    that the new gates read are derived from the SAME fills (as they are in real optimize())."""
    return summarize(trades)


# Multi-ticker OOS books that clear the new gates: the winner beats the incumbent by a fat,
# consistent margin across >= _CLUSTER_FLOOR distinct tickers, so the clustered two-sample delta
# lower bound is well above 0 and a random relabel cannot reproduce the edge.
_WINNER_TICKERS = {f"T{i}": [1.2, 1.4, 1.0, 1.3] for i in range(10)}      # mean ~1.2R, 10 tickers
_INCUMBENT_TICKERS = {f"T{i}": [0.0, -0.2, 0.1, -0.1] for i in range(10)}  # mean ~ -0.05R


def _result(*, winner, in_sample, out_of_sample, out_of_sample_trades=None):
    return OptimizeResult(
        in_sample=in_sample,
        out_of_sample=out_of_sample,
        winner=winner,
        out_of_sample_trades=out_of_sample_trades or {},
    )


def test_proposes_when_winner_beats_incumbent_out_of_sample():
    # ext_1.5 wins in-sample AND holds out-of-sample on a trusted, positive, MULTI-TICKER book
    # that beats the incumbent (ext_2.0): trusted n, positive lower bound, beats incumbent on
    # point estimate, spans 10 tickers, clustered delta low > 0, placebo cleared -> a proposal.
    win_trades = _trades(_WINNER_TICKERS, "ext_1.5")
    inc_trades = _trades(_INCUMBENT_TICKERS, "ext_2.0")
    result = _result(
        winner="ext_1.5",
        in_sample={"ext_1.5": _summary([1.0] * 30), "ext_2.0": _summary([0.5] * 30)},
        out_of_sample={"ext_1.5": _summary_of(win_trades), "ext_2.0": _summary_of(inc_trades)},
        out_of_sample_trades={"ext_1.5": win_trades, "ext_2.0": inc_trades},
    )
    p = propose(result, BASE, min_oos_trades=20)
    assert isinstance(p, Proposal)
    assert p.knob == "max_extension_atr" and p.current == 2.0 and p.proposed == 1.5
    assert "2.0 → 1.5" in p.title
    # Provenance: a grid hash, a git SHA, and the configuration count are recorded in the body.
    assert "grid:" in p.body and "sha:" in p.body and "n_configs=" in p.body


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


def test_no_proposal_when_clustered_two_sample_delta_lower_bound_includes_zero():
    # (a) The winner beats the incumbent on the OOS POINT estimate (1.0R vs 0.9R), spans 10
    # tickers, and its OWN out-of-sample lower bound is positive (so the existing ci_low gate
    # passes) -- but the two books' per-ticker means are HETEROGENEOUS and heavily OVERLAPPING, so
    # resampling tickers INDEPENDENTLY in each book produces a winner-minus-incumbent delta whose
    # lower 2.5% bound dips below 0. The edge is not certifiable as a difference. No proposal.
    # (Isolates the clustered TWO-SAMPLE delta gate: every earlier gate -- trusted n, positive
    # own ci_low, beats incumbent, >= _CLUSTER_FLOOR tickers -- passes; only the delta fails.)
    win_means = [0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 1.0]   # book mean 1.0R
    inc_means = [0.1, 0.3, 0.5, 0.7, 0.9, 1.1, 1.3, 1.5, 1.7, 0.9]   # book mean 0.9R
    win = {f"T{i}": [m, m] for i, m in enumerate(win_means)}   # tight within ticker, spread across
    inc = {f"T{i}": [m, m] for i, m in enumerate(inc_means)}
    win_trades, inc_trades = _trades(win, "ext_1.5"), _trades(inc, "ext_2.0")
    result = _result(
        winner="ext_1.5",
        in_sample={"ext_1.5": _summary([1.0] * 30), "ext_2.0": _summary([0.5] * 30)},
        out_of_sample={"ext_1.5": _summary_of(win_trades), "ext_2.0": _summary_of(inc_trades)},
        out_of_sample_trades={"ext_1.5": win_trades, "ext_2.0": inc_trades},
    )
    assert propose(result, BASE, min_oos_trades=20) is None


def test_no_proposal_when_winner_spans_too_few_distinct_tickers():
    # (b) A genuinely strong, well-separated edge -- but concentrated on FEWER than _CLUSTER_FLOOR
    # distinct tickers. Too few clusters to certify; no proposal even though the delta is fat.
    n = _CLUSTER_FLOOR - 1
    win = {f"T{i}": [1.5] * 4 for i in range(n)}      # strong, but only 7 tickers
    inc = {f"T{i}": [0.0] * 4 for i in range(n)}
    win_trades, inc_trades = _trades(win, "ext_1.5"), _trades(inc, "ext_2.0")
    result = _result(
        winner="ext_1.5",
        in_sample={"ext_1.5": _summary([1.0] * 30), "ext_2.0": _summary([0.5] * 30)},
        out_of_sample={"ext_1.5": _summary_of(win_trades), "ext_2.0": _summary_of(inc_trades)},
        out_of_sample_trades={"ext_1.5": win_trades, "ext_2.0": inc_trades},
    )
    assert propose(result, BASE, min_oos_trades=20) is None


def test_no_proposal_when_placebo_is_not_cleared():
    # (c) Winner beats incumbent on the point estimate (1.1R vs 1.0R), spans 10 tickers, and the
    # clustered delta lower bound is comfortably positive (every ticker edges the incumbent by the
    # same 0.1R, so the resampled delta barely varies). BUT each book has a WIDE within-ticker
    # spread (3.0 / -0.8), so the pooled relabel null is wide and the small 0.1R observed delta
    # does NOT exceed the shuffled null's 95th percentile: a random relabel reproduces the edge, so
    # it is an artifact, not signal. Placebo fails -> no proposal. (Isolates the placebo gate: the
    # ticker floor and the clustered delta gate BOTH pass; only the placebo rejects.)
    win = {f"T{i}": [3.0, -0.8] for i in range(10)}   # mean 1.1R, wide spread
    inc = {f"T{i}": [2.9, -0.9] for i in range(10)}   # mean 1.0R, wide spread
    win_trades, inc_trades = _trades(win, "ext_1.5"), _trades(inc, "ext_2.0")
    result = _result(
        winner="ext_1.5",
        in_sample={"ext_1.5": _summary([1.0] * 30), "ext_2.0": _summary([0.5] * 30)},
        out_of_sample={"ext_1.5": _summary_of(win_trades), "ext_2.0": _summary_of(inc_trades)},
        out_of_sample_trades={"ext_1.5": win_trades, "ext_2.0": inc_trades},
    )
    assert propose(result, BASE, min_oos_trades=20) is None


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

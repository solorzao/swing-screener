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


def _result(*, winner, in_sample, out_of_sample, out_of_sample_trades=None, scopes=None):
    return OptimizeResult(
        in_sample=in_sample,
        out_of_sample=out_of_sample,
        winner=winner,
        out_of_sample_trades=out_of_sample_trades or {},
        scopes=scopes or {},
    )


def test_gates_slice_to_the_winners_play_type_scope():
    """A continuation-knob winner must be judged on continuation fills ONLY. Here the
    winner's apparent edge lives entirely in REVERSAL fills -- a population its knob
    (max_extension_atr) cannot touch, i.e. an artifact. The pooled (legacy) view
    promotes it; the scoped gates must not (2026-07 review: play-type slicing)."""
    flat = {f"T{i}": [0.0, 0.1, -0.1, 0.0] for i in range(10)}   # ~0R continuation, both books
    win_cont = _trades(flat, "ext_1.5")
    inc_cont = _trades(flat, "ext_2.0")
    win_rev = _trades(_WINNER_TICKERS, "ext_1.5")                # fat +1.2R cohort...
    for t in win_rev:
        t.play_type = "reversal"                                  # ...on the UNTOUCHED path
    win_all = win_cont + win_rev

    # Legacy pooled shape (no scopes): the reversal artifact carries the pooled summary
    # and every gate -> a FALSE promotion. This documents the failure the fix removes.
    pooled = _result(
        winner="ext_1.5",
        in_sample={"ext_1.5": _summary_of(win_all), "ext_2.0": _summary_of(inc_cont)},
        out_of_sample={"ext_1.5": _summary_of(win_all), "ext_2.0": _summary_of(inc_cont)},
        out_of_sample_trades={"ext_1.5": win_all, "ext_2.0": inc_cont},
    )
    assert isinstance(propose(pooled, BASE, min_oos_trades=20), Proposal)

    # Scoped shape (what optimize() now produces): the winner's summary line is its
    # CONTINUATION book (~0R) and the trade-level gates slice both books the same way
    # -> the reversal artifact is invisible and nothing is proposed.
    scoped = _result(
        winner="ext_1.5",
        in_sample={"ext_1.5": _summary_of(win_cont), "ext_2.0": _summary_of(inc_cont)},
        out_of_sample={"ext_1.5": _summary_of(win_cont), "ext_2.0": _summary_of(inc_cont)},
        out_of_sample_trades={"ext_1.5": win_all, "ext_2.0": inc_cont},
        scopes={"ext_1.5": "continuation", "ext_2.0": "continuation"},
    )
    assert propose(scoped, BASE, min_oos_trades=20) is None


def test_queued_variant_win_is_loud_but_never_auto_applied(caplog):
    """An analyst-QUEUED variant (grid key "proposed:*") that wins the sweep AND clears
    every promotion gate must NOT be auto-applied -- apply_to_config edits the gate knob
    only, and promotion of an arbitrary analyst delta stays a human act. It must also not
    CRASH (the old code parsed float("proposed:...") -> ValueError = a red weekly run).
    The win is surfaced as a WARNING so it can't pass silently."""
    import logging

    win_trades = _trades(_WINNER_TICKERS, "proposed:rev_lowvol_gate")
    inc_trades = _trades(_INCUMBENT_TICKERS, "ext_2.0")
    result = _result(
        winner="proposed:rev_lowvol_gate",
        in_sample={"proposed:rev_lowvol_gate": _summary_of(win_trades),
                   "ext_2.0": _summary_of(inc_trades)},
        out_of_sample={"proposed:rev_lowvol_gate": _summary_of(win_trades),
                       "ext_2.0": _summary_of(inc_trades)},
        out_of_sample_trades={"proposed:rev_lowvol_gate": win_trades,
                              "ext_2.0": inc_trades},
    )
    with caplog.at_level(logging.WARNING, logger="swing_screener.pipeline.propose"):
        assert propose(result, BASE) is None  # no crash, no auto-edit
    assert any("proposed:rev_lowvol_gate" in r.message and "human" in r.message.lower()
               for r in caplog.records)


def test_grid_with_queued_merges_the_reflection_queue(tmp_path):
    """The reflection-to-optimizer handoff: queued variants in edge/*.proposed.json join
    the swept grid under namespaced keys, alongside the standard gate sweep."""
    import json

    from swing_screener.pipeline.propose import _grid_with_queued

    edge = tmp_path / "edge"
    edge.mkdir()
    (edge / "reversal.proposed.json").write_text(json.dumps([{
        "name": "rev_min_pullback", "play_type": "reversal",
        "delta": {"min_pullback_bars": 3}, "rationale": "r", "hunch_ref": "h",
        "status": "queued", "drafted_at": "2026-07-02", "provenance": "reflection-opus",
    }]), encoding="utf-8")

    grid, scopes = _grid_with_queued(BASE, edge)
    assert "proposed:rev_min_pullback" in grid
    assert grid["proposed:rev_min_pullback"].min_pullback_bars == 3
    assert any(k.startswith("ext_") for k in grid)  # the standard sweep is still there
    # scopes: the gate sweep scores on continuation; the queued variant on ITS play type
    assert scopes["ext_2.0"] == "continuation"
    assert scopes["proposed:rev_min_pullback"] == "reversal"


def test_grid_with_queued_is_plain_sweep_when_nothing_queued(tmp_path):
    from swing_screener.pipeline.propose import _grid_with_queued

    edge = tmp_path / "edge"
    edge.mkdir()  # no proposed.json files
    grid, scopes = _grid_with_queued(BASE, edge)
    assert all(not k.startswith("proposed:") for k in grid)
    assert any(k.startswith("ext_") for k in grid)
    assert all(v == "continuation" for v in scopes.values())


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


def test_main_exits_nonzero_when_no_data_fetched(monkeypatch, tmp_path):
    """A data outage must show RED on the scheduled optimize run, not a green no-op
    (2026-07-01 audit): main() raises SystemExit(1) when fetch_daily yields nothing."""
    import pytest

    from swing_screener.pipeline import propose as propose_mod

    monkeypatch.setattr(propose_mod, "fetch_daily", lambda tickers, cache_dir: {})
    monkeypatch.setattr(
        "sys.argv",
        ["propose", "--tickers", "AMD,NVDA", "--cache-dir", str(tmp_path)],
    )
    with pytest.raises(SystemExit) as exc:
        propose_mod.main()
    assert exc.value.code == 1

"""Fill-pessimism haircut on LEVEL exits (stop/target/partial) -- Phase-0 Task 2.

The shadow book records stop/target/partial fills slightly favourably (a clean
fill exactly at the level). ``fill_slippage_atr`` lets us pessimise that fill by
``k * ATR`` so the optimizer stops chasing an optimistic-fill UPPER BOUND. The
EXIT DECISION (which bar trips which level) is unchanged -- only the recorded
fill PRICE moves. momentum_flip/time_stop exits use the bar close and are NOT
haircut. Default 0.0 must be a strict, byte-identical no-op.

The default fixture fills at the ceiling against a (105.0, 97.0) next bar, so
every opened trade has entry=101.0, stop=94.0, risk=7.0, target=110.0.
"""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import repo
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.shadow import FillCandidate, advance_open, open_from_signals
from swing_screener.signals.entry_zone import EntryZone

ZONE = EntryZone(floor=96.0, ceiling=101.0, stop=94.0, target=110.0, risk=4.0, reference=98.5)

ENTRY = 101.0
RISK = 7.0
STOP = 94.0
TARGET = 110.0
ATR = 2.0
K = 0.5  # slip = K * ATR = 1.0


def _cand(ticker="AAPL"):
    return FillCandidate(ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8,
                         rank=1, mtf_aligned=True, signal_id=None, zone=ZONE)


def _open_default(s):
    """Open the default candidate (entry 101, stop 94, risk 7, target 110)."""
    open_from_signals(s, [_cand()], {("AAPL", "1d"): (105.0, 97.0)}, fill_date=date(2024, 1, 3))
    pt = repo.load_open_paper_trades(s)[0]
    assert pt.entry_price == ENTRY and pt.risk == RISK and pt.target == TARGET
    return pt


def _all_paper_trades(session):
    return list(session.scalars(select(PaperTrade)))


# --- (a) stop exit: a bigger loss by exactly k*atr/risk -----------------------


def test_stop_exit_haircut_widens_loss_by_k_atr_over_risk():
    cfg = StrategyConfig(fill_slippage_atr=K)
    bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False, "atr": ATR}
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        # The exit DECISION is still "stop" (low 93 <= stop 94); only the fill price
        # is pessimised by slip = K*ATR = 1.0 -> exit at 93.0, not 94.0.
        assert closed.exit_reason == "stop"
        assert closed.exit_price == STOP - K * ATR
        baseline_r = (STOP - ENTRY) / RISK
        assert closed.realized_r == pytest.approx(baseline_r - K * ATR / RISK)


def test_stop_exit_byte_identical_at_zero():
    cfg = StrategyConfig(fill_slippage_atr=0.0)
    bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False, "atr": ATR}
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.exit_reason == "stop"
        assert closed.exit_price == STOP
        assert closed.realized_r == (STOP - ENTRY) / RISK


# --- (b) all-or-nothing target exit: a smaller gain by exactly k*atr/risk ------


def test_target_exit_haircut_shrinks_gain_by_k_atr_over_risk():
    # partial_frac=0.0 (default) -> the target is a terminal all-or-nothing exit.
    cfg = StrategyConfig(fill_slippage_atr=K)
    assert cfg.partial_frac == 0.0
    bar = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False, "atr": ATR}
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.exit_reason == "target" and closed.partial_done is False
        assert closed.exit_price == TARGET - K * ATR
        baseline_r = (TARGET - ENTRY) / RISK
        assert closed.realized_r == pytest.approx(baseline_r - K * ATR / RISK)


def test_target_exit_byte_identical_at_zero():
    cfg = StrategyConfig(fill_slippage_atr=0.0)
    bar = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False, "atr": ATR}
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.exit_reason == "target"
        assert closed.exit_price == TARGET
        assert closed.realized_r == (TARGET - ENTRY) / RISK


# --- (c) partial path: partial leg + size-weighted final R both haircut --------


def test_partial_leg_price_and_r_are_haircut():
    cfg = StrategyConfig(partial_frac=0.33, fill_slippage_atr=K)
    bar = {"low": 100.0, "high": 111.0, "close": 108.0, "shaved_head": False, "atr": ATR}
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        pt = s.get(PaperTrade, pt.id)
        assert pt.status == "open" and pt.partial_done is True
        # the partial leg fills at target - slip, not the exact target
        assert pt.partial_price == TARGET - K * ATR
        assert pt.partial_r == pytest.approx((TARGET - K * ATR - ENTRY) / RISK)


def test_partial_then_stop_size_weighted_realized_r_drops_by_k_atr_over_risk():
    # The size-weighted realized_r on the runner's final (stop) close drops by
    # exactly k*atr/risk vs the k=0 baseline: BOTH the partial leg AND the runner's
    # stop fill are level fills, so both are pessimised by the same slip.
    bar1 = {"low": 100.0, "high": 111.0, "close": 108.0, "shaved_head": False, "atr": ATR}
    bar2 = {"low": 99.0, "high": 103.0, "close": 102.0, "shaved_head": False, "atr": ATR}

    def run(k):
        cfg = StrategyConfig(partial_frac=0.33, fill_slippage_atr=k)
        engine = get_engine("sqlite:///:memory:")
        with Session(engine) as s:
            pt = _open_default(s)
            advance_open(s, {("AAPL", "1d"): bar1}, cfg, today=date(2024, 1, 4))
            # bar2 dips to the breakeven stop (101.0); with the haircut the runner
            # stop still TRIPS at 101.0 (low 99 <= 101) but fills at 101 - slip.
            advance_open(s, {("AAPL", "1d"): bar2}, cfg, today=date(2024, 1, 5))
            closed = s.get(PaperTrade, pt.id)
            assert closed.exit_reason == "stop"
            return closed.realized_r

    baseline = run(0.0)
    haircut = run(K)
    # Partial weight (1 - remaining_frac) = 0.33, runner weight = 0.67. Each leg is
    # a level fill displaced by slip/risk in R; the size weights sum to 1.0, so the
    # blended realized_r drops by exactly k*atr/risk.
    assert haircut == pytest.approx(baseline - K * ATR / RISK)


def test_partial_path_byte_identical_at_zero():
    cfg = StrategyConfig(partial_frac=0.33, fill_slippage_atr=0.0)
    bar = {"low": 100.0, "high": 111.0, "close": 108.0, "shaved_head": False, "atr": ATR}
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        pt = s.get(PaperTrade, pt.id)
        assert pt.partial_price == TARGET
        assert pt.partial_r == (TARGET - ENTRY) / RISK


# --- (d) close-based exits (momentum_flip / time_stop) are NOT haircut ---------


def test_momentum_flip_exit_is_not_haircut():
    cfg = StrategyConfig(fill_slippage_atr=K)
    bar = {"low": 100.0, "high": 104.0, "close": 102.5, "shaved_head": True, "atr": ATR}
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.exit_reason == "momentum_flip"
        # close-based fill -> unchanged by the haircut
        assert closed.exit_price == 102.5
        assert closed.realized_r == (102.5 - ENTRY) / RISK


def test_time_stop_exit_is_not_haircut():
    cfg = StrategyConfig(fill_slippage_atr=K)
    bar = {"low": 100.0, "high": 104.0, "close": 103.0, "shaved_head": False, "atr": ATR}
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        pt.hold_bars = 9   # 1d time stop = 10 bars; this bar is the 10th -> time_stop
        s.commit()
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.exit_reason == "time_stop"
        assert closed.exit_price == 103.0
        assert closed.realized_r == (103.0 - ENTRY) / RISK


# --- (e) atr guard: k>0 but undefined/zero ATR -> no haircut, no NaN -----------


def test_no_haircut_when_atr_is_zero():
    cfg = StrategyConfig(fill_slippage_atr=K)
    bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False, "atr": 0.0}
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.exit_reason == "stop"
        assert closed.exit_price == STOP                  # exact level, no haircut
        assert closed.realized_r == (STOP - ENTRY) / RISK


def test_no_haircut_when_atr_missing():
    cfg = StrategyConfig(fill_slippage_atr=K)
    bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False}  # no "atr"
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.exit_reason == "stop"
        assert closed.exit_price == STOP                  # exact level, no NaN
        assert closed.realized_r == (STOP - ENTRY) / RISK

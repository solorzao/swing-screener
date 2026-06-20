"""Optional ATR fill-pessimism haircut on LEVEL-based exits (stop/target).

The haircut (``exit_slippage_atr`` fraction of the bar ATR) makes a LONG's
stop fill LOWER (bigger loss) and target fill LOWER (smaller gain). It must:

* be a strict no-op at the default 0.0 (covered by the rest of the suite, which
  runs unchanged),
* NOT change the exit DECISION (whether the level was touched), only the fill
  price, and
* be skipped when the bar ATR is 0/missing (no NaN / spurious shift).

These tests compare a k=0 run and a k>0 run on identical setups and assert the
realized_r drops by exactly k*atr/risk on the haircut leg(s).
"""

from datetime import date

import pytest
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import repo
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.shadow import FillCandidate, advance_open, open_from_signals
from swing_screener.signals.entry_zone import EntryZone

ZONE = EntryZone(floor=96.0, ceiling=101.0, stop=94.0, target=110.0, risk=4.0, reference=98.5)


def _cand(ticker="AAPL"):
    return FillCandidate(ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8,
                         rank=1, mtf_aligned=True, signal_id=None, zone=ZONE)


def _open_default(s):
    """Open the default candidate (entry 101, stop 94, risk 7, target 110)."""
    open_from_signals(s, [_cand()], {("AAPL", "1d"): (105.0, 97.0)}, fill_date=date(2024, 1, 3))
    pt = repo.load_open_paper_trades(s)[0]
    assert pt.entry_price == 101.0 and pt.risk == 7.0 and pt.target == 110.0
    return pt


def test_stop_exit_haircut_widens_the_loss():
    # A stop-out bar with a known ATR. k=0 fills at the stop (94.0); k>0 fills at
    # 94.0 - k*atr, a bigger loss. realized_r drops by exactly k*atr/risk.
    k = 0.5
    atr = 3.0
    risk = 7.0
    bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False, "atr": atr}

    # k=0 baseline (exact-level fill)
    engine0 = get_engine("sqlite:///:memory:")
    with Session(engine0) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, StrategyConfig(exit_slippage_atr=0.0),
                     today=date(2024, 1, 4))
        base = s.get(PaperTrade, pt.id)
        assert base.exit_reason == "stop" and base.exit_price == 94.0
        base_r = base.realized_r

    # k>0 haircut
    engine1 = get_engine("sqlite:///:memory:")
    with Session(engine1) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, StrategyConfig(exit_slippage_atr=k),
                     today=date(2024, 1, 4))
        cut = s.get(PaperTrade, pt.id)
        # DECISION unchanged: still a stop on the same bar
        assert cut.exit_reason == "stop"
        # fill moved DOWN by k*atr; realized_r is lower by k*atr/risk
        assert cut.exit_price == pytest.approx(94.0 - k * atr)
        assert cut.realized_r == pytest.approx(base_r - k * atr / risk)
        assert cut.realized_r < base_r   # a bigger loss


def test_target_exit_haircut_shrinks_the_gain_all_or_nothing():
    # An all-or-nothing arm (partial_frac=0.0) that hits the target. k>0 fills at
    # 110.0 - k*atr, a smaller gain; realized_r drops by k*atr/risk.
    k = 0.5
    atr = 3.0
    risk = 7.0
    bar = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False, "atr": atr}

    engine0 = get_engine("sqlite:///:memory:")
    with Session(engine0) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, StrategyConfig(exit_slippage_atr=0.0),
                     today=date(2024, 1, 4))
        base = s.get(PaperTrade, pt.id)
        assert base.exit_reason == "target" and base.exit_price == 110.0
        assert base.partial_done is False
        base_r = base.realized_r

    engine1 = get_engine("sqlite:///:memory:")
    with Session(engine1) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, StrategyConfig(exit_slippage_atr=k),
                     today=date(2024, 1, 4))
        cut = s.get(PaperTrade, pt.id)
        assert cut.exit_reason == "target"   # DECISION unchanged
        assert cut.exit_price == pytest.approx(110.0 - k * atr)
        assert cut.realized_r == pytest.approx(base_r - k * atr / risk)
        assert cut.realized_r < base_r   # a smaller gain


def test_momentum_flip_exit_gets_no_haircut():
    # momentum_flip exits use the bar CLOSE (already realistic) -> NO haircut even
    # with k>0: the exit price and realized_r are identical to k=0.
    k = 0.5
    atr = 3.0
    bar = {"low": 100.0, "high": 104.0, "close": 102.5, "shaved_head": True, "atr": atr}

    engine0 = get_engine("sqlite:///:memory:")
    with Session(engine0) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, StrategyConfig(exit_slippage_atr=0.0),
                     today=date(2024, 1, 4))
        base = s.get(PaperTrade, pt.id)
        assert base.exit_reason == "momentum_flip"
        base_r = base.realized_r

    engine1 = get_engine("sqlite:///:memory:")
    with Session(engine1) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, StrategyConfig(exit_slippage_atr=k),
                     today=date(2024, 1, 4))
        cut = s.get(PaperTrade, pt.id)
        assert cut.exit_reason == "momentum_flip"
        assert cut.exit_price == 102.5            # bar close, unchanged
        assert cut.realized_r == pytest.approx(base_r)   # no haircut


def test_partial_path_haircut_on_partial_price_and_runner():
    # A partial arm (partial_frac>0). The target touch books the FIRST leg at
    # partial_price = target - k*atr (a smaller booked gain), and the runner later
    # stops at breakeven. The size-weighted realized_r reflects the haircut partial_r.
    k = 0.5
    atr = 2.0
    risk = 7.0
    frac = 0.33
    bar1 = {"low": 100.0, "high": 111.0, "close": 108.0, "shaved_head": False,
            "shaved_bottom": False, "atr": atr}
    bar2 = {"low": 100.0, "high": 103.0, "close": 102.0, "shaved_head": False,
            "shaved_bottom": False, "atr": atr}

    # k=0 baseline
    engine0 = get_engine("sqlite:///:memory:")
    with Session(engine0) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar1}, StrategyConfig(partial_frac=frac, exit_slippage_atr=0.0),
                     today=date(2024, 1, 4))
        pt = s.get(PaperTrade, pt.id)
        assert pt.partial_done is True and pt.partial_price == 110.0
        base_partial_r = pt.partial_r
        advance_open(s, {("AAPL", "1d"): bar2}, StrategyConfig(partial_frac=frac, exit_slippage_atr=0.0),
                     today=date(2024, 1, 5))
        base = s.get(PaperTrade, pt.id)
        assert base.exit_reason == "stop" and base.exit_price == 101.0  # breakeven stop, no haircut shift below
        base_r = base.realized_r

    # k>0 haircut
    engine1 = get_engine("sqlite:///:memory:")
    with Session(engine1) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar1}, StrategyConfig(partial_frac=frac, exit_slippage_atr=k),
                     today=date(2024, 1, 4))
        pt = s.get(PaperTrade, pt.id)
        # partial leg booked LOWER by k*atr
        assert pt.partial_done is True
        assert pt.partial_price == pytest.approx(110.0 - k * atr)
        assert pt.partial_r == pytest.approx(base_partial_r - k * atr / risk)
        assert pt.partial_r == pytest.approx((110.0 - k * atr - 101.0) / risk)
        # the runner's breakeven stop sits at 101.0; the haircut on THIS exit pushes
        # the runner fill to 101.0 - k*atr too (a small extra loss on the runner leg)
        advance_open(s, {("AAPL", "1d"): bar2}, StrategyConfig(partial_frac=frac, exit_slippage_atr=k),
                     today=date(2024, 1, 5))
        cut = s.get(PaperTrade, pt.id)
        assert cut.exit_reason == "stop"
        assert cut.exit_price == pytest.approx(101.0 - k * atr)
        # size-weighted realized_r: both legs are haircut by k*atr/risk on their R,
        # so the whole realized_r drops by exactly k*atr/risk (weights sum to 1.0).
        assert cut.realized_r == pytest.approx(base_r - k * atr / risk)


def test_haircut_skipped_when_atr_zero():
    # exit_slippage_atr > 0 but the bar ATR is 0.0 -> slip resolves to 0.0 (the guard),
    # so the fill is the exact level: identical to the no-haircut run, no NaN.
    bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False, "atr": 0.0}
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, StrategyConfig(exit_slippage_atr=0.5),
                     today=date(2024, 1, 4))
        cut = s.get(PaperTrade, pt.id)
        assert cut.exit_reason == "stop"
        assert cut.exit_price == 94.0   # exact level, ATR=0 -> no haircut
        assert cut.realized_r == pytest.approx((94.0 - 101.0) / 7.0)


def test_haircut_skipped_when_atr_missing():
    # ATR field absent from the bar -> atr_val defaults to 0.0 -> guard skips the
    # haircut. No KeyError, no NaN poisoning.
    bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False}
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        advance_open(s, {("AAPL", "1d"): bar}, StrategyConfig(exit_slippage_atr=0.5),
                     today=date(2024, 1, 4))
        cut = s.get(PaperTrade, pt.id)
        assert cut.exit_reason == "stop"
        assert cut.exit_price == 94.0
        assert cut.realized_r == pytest.approx((94.0 - 101.0) / 7.0)

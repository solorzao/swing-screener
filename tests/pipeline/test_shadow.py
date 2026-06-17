from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import breakdown
from swing_screener.config import StrategyConfig
from swing_screener.db import repo
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import build_arms
from swing_screener.pipeline.shadow import (
    FillCandidate,
    _is_softening,
    advance_open,
    open_from_signals,
)
from swing_screener.signals.entry_zone import EntryZone

CFG = StrategyConfig()
ZONE = EntryZone(floor=96.0, ceiling=101.0, stop=94.0, target=110.0, risk=4.0, reference=98.5)


def _cand(ticker="AAPL"):
    return FillCandidate(ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8,
                         rank=1, mtf_aligned=True, signal_id=None, zone=ZONE)


def _all_paper_trades(session):
    return list(session.scalars(select(PaperTrade)))


def test_nonpositive_risk_fill_is_downgraded_to_invalidated():
    # a degenerate zone whose worst-case fill sits at/below the stop yields
    # risk <= 0; it must be downgraded to invalidated (never opened), so the
    # later realized_r division can never hit a zero divisor.
    engine = get_engine("sqlite:///:memory:")
    degenerate = EntryZone(floor=100.0, ceiling=101.0, stop=101.0, target=110.0,
                           risk=0.0, reference=100.5)
    cand = FillCandidate(ticker="AAPL", timeframe="1d", horizon="medium", signal_score=0.8,
                         rank=1, mtf_aligned=True, signal_id=None, zone=degenerate)
    with Session(engine) as s:
        open_from_signals(s, [cand], {("AAPL", "1d"): (100.5, 100.0)}, fill_date=date(2024, 1, 3))
        assert repo.load_open_paper_trades(s) == []
        pt = _all_paper_trades(s)[0]
        assert pt.fill_status == "invalidated" and pt.entry_price is None


def test_missed_when_next_bar_gaps_above_ceiling():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_cand()], {("AAPL", "1d"): (120.0, 102.0)}, fill_date=date(2024, 1, 3))
        assert repo.load_open_paper_trades(s) == []  # missed -> not open

        rows = _all_paper_trades(s)
        assert len(rows) == 1
        pt = rows[0]
        assert pt.fill_status == "missed"
        assert pt.status == "closed"
        assert pt.entry_price is None


def test_fill_then_hard_stop_closes_with_negative_r():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        # next bar trades through the zone -> worst-case fill at ceiling (101.0)
        open_from_signals(s, [_cand()], {("AAPL", "1d"): (105.0, 97.0)}, fill_date=date(2024, 1, 3))
        opened = repo.load_open_paper_trades(s)
        assert len(opened) == 1
        pt = opened[0]
        assert pt.entry_price == 101.0 and pt.hold_bars == 0
        assert pt.risk == 101.0 - 94.0  # entry - stop

        # a bar that breaches the stop -> hard-stop exit
        bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False, "bearish": True}
        advance_open(s, {("AAPL", "1d"): bar}, CFG, today=date(2024, 1, 4))
        closed = s.get(type(pt), pt.id)
        assert closed.status == "closed" and closed.exit_reason == "stop"
        assert closed.exit_price == 94.0
        assert closed.realized_r < 0           # ~ -1R
        assert repo.load_open_paper_trades(s) == []


def test_hold_increments_bars_held():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_cand()], {("AAPL", "1d"): (105.0, 97.0)}, fill_date=date(2024, 1, 3))
        quiet = {"low": 99.0, "high": 103.0, "close": 100.0, "shaved_head": False, "bearish": False}
        advance_open(s, {("AAPL", "1d"): quiet}, CFG, today=date(2024, 1, 4))
        pt = repo.load_open_paper_trades(s)[0]
        assert pt.hold_bars == 1 and pt.status == "open"


def test_advance_skips_trade_opened_today():
    # a trade must never be advanced on the same bar it was filled on.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_cand()], {("AAPL", "1d"): (105.0, 97.0)}, fill_date=date(2024, 1, 3))
        quiet = {"low": 99.0, "high": 103.0, "close": 100.0, "shaved_head": False, "bearish": False}
        advance_open(s, {("AAPL", "1d"): quiet}, CFG, today=date(2024, 1, 3))
        pt = repo.load_open_paper_trades(s)[0]
        assert pt.status == "open" and pt.hold_bars == 0  # not advanced on its own entry bar


def test_advance_is_idempotent_same_day():
    # re-running advance for the same day must not double-count the bar.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_cand()], {("AAPL", "1d"): (105.0, 97.0)}, fill_date=date(2024, 1, 3))
        quiet = {"low": 99.0, "high": 103.0, "close": 100.0, "shaved_head": False, "bearish": False}
        advance_open(s, {("AAPL", "1d"): quiet}, CFG, today=date(2024, 1, 4))
        assert repo.load_open_paper_trades(s)[0].hold_bars == 1
        advance_open(s, {("AAPL", "1d"): quiet}, CFG, today=date(2024, 1, 4))
        assert repo.load_open_paper_trades(s)[0].hold_bars == 1  # already advanced today


def test_open_records_categorization_tags():
    # tags are denormalized onto the paper trade so the shadow book is sliceable.
    engine = get_engine("sqlite:///:memory:")
    cand = FillCandidate(ticker="AAPL", timeframe="1d", horizon="medium", signal_score=0.8,
                         rank=1, mtf_aligned=True, signal_id=None, zone=ZONE,
                         quality_tier="reputable", volatility_tier="high", oversold=True)
    with Session(engine) as s:
        open_from_signals(s, [cand], {("AAPL", "1d"): (105.0, 97.0)}, fill_date=date(2024, 1, 3))
        pt = repo.load_open_paper_trades(s)[0]
        assert pt.quality_tier == "reputable"
        assert pt.volatility_tier == "high"
        assert pt.oversold is True


# --- fractional-close (Step B) ----------------------------------------------
#
# The default fixture fills at the ceiling (101.0) against a (105.0, 97.0) next
# bar, so every opened trade below has entry=101.0, stop=94.0, risk=7.0,
# target=110.0 unless noted.


def _open_default(s):
    """Open the default candidate (entry 101, stop 94, risk 7, target 110)."""
    open_from_signals(s, [_cand()], {("AAPL", "1d"): (105.0, 97.0)}, fill_date=date(2024, 1, 3))
    pt = repo.load_open_paper_trades(s)[0]
    assert pt.entry_price == 101.0 and pt.risk == 7.0 and pt.target == 110.0
    return pt


def test_reconciliation_realized_r_unchanged_at_frac_zero():
    # With the feature OFF (partial_frac=0.0, the default), realized_r must equal
    # the all-or-nothing (exit_price - entry)/risk for every exit reason, exactly
    # as before the refactor. entry=101.0, stop=94.0, risk=7.0, target=110.0.
    cfg = StrategyConfig()
    assert cfg.partial_frac == 0.0

    # (a) target hit -> exit at target 110.0 -> +9/7 R
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        bar = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False}
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.exit_reason == "target" and closed.exit_price == 110.0
        assert closed.partial_done is False
        assert closed.realized_r == (110.0 - 101.0) / 7.0

    # (b) hard stop -> exit at stop 94.0 -> -1R
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False}
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.exit_reason == "stop" and closed.exit_price == 94.0
        assert closed.realized_r == (94.0 - 101.0) / 7.0

    # (c) momentum_flip (shaved_head) -> exit at close
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        bar = {"low": 100.0, "high": 104.0, "close": 102.5, "shaved_head": True}
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.exit_reason == "momentum_flip" and closed.exit_price == 102.5
        assert closed.realized_r == (102.5 - 101.0) / 7.0

    # (d) time_stop -> exit at close (1d time stop = 10 bars; jump hold_bars to 9
    #     so this bar is the 10th and trips the limit, without stop/flip/target).
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        pt.hold_bars = 9
        s.commit()
        bar = {"low": 100.0, "high": 104.0, "close": 103.0, "shaved_head": False}
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.exit_reason == "time_stop" and closed.exit_price == 103.0
        assert closed.realized_r == (103.0 - 101.0) / 7.0


def test_partial_then_breakeven_stop_size_weights_realized_r():
    # Feature ON (partial_frac=0.33): a bar hits the target -> scale-out, NOT a
    # terminal exit. The first leg books partial_r=(110-101)/7, remaining_frac
    # drops to 0.67, and the stop ratchets to breakeven (entry 101.0). A later bar
    # that breaches the breakeven stop closes the runner at final_r=0.0, so
    # realized_r == 0.33*partial_r + 0.67*0.0.
    cfg = StrategyConfig(partial_frac=0.33)
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)

        # bar 1: high pierces the target -> partial scale-out
        bar1 = {"low": 100.0, "high": 111.0, "close": 108.0, "shaved_head": False}
        advance_open(s, {("AAPL", "1d"): bar1}, cfg, today=date(2024, 1, 4))
        pt = s.get(PaperTrade, pt.id)
        assert pt.status == "open"            # NOT terminal
        assert pt.partial_done is True
        assert pt.partial_price == 110.0
        assert pt.partial_r == (110.0 - 101.0) / 7.0
        assert pt.remaining_frac == pytest.approx(1.0 - 0.33)
        assert pt.stop == 101.0               # moved to breakeven (= entry)
        assert pt.exit_reason is None

        # bar 2: dips to the breakeven stop -> runner closes at final_r = 0
        bar2 = {"low": 100.0, "high": 103.0, "close": 102.0, "shaved_head": False}
        advance_open(s, {("AAPL", "1d"): bar2}, cfg, today=date(2024, 1, 5))
        closed = s.get(PaperTrade, pt.id)
        assert closed.status == "closed" and closed.exit_reason == "stop"
        assert closed.exit_price == 101.0
        partial_r = (110.0 - 101.0) / 7.0
        assert closed.realized_r == pytest.approx(0.33 * partial_r + 0.67 * 0.0)


def test_partial_then_runner_flips_above_entry():
    # Feature ON: partial at the target, then the runner exits on a momentum_flip
    # ABOVE breakeven (close 105.0 > entry 101.0) -> positive runner R.
    # realized_r == 0.33*partial_r + 0.67*final_r.
    cfg = StrategyConfig(partial_frac=0.33)
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)

        bar1 = {"low": 100.0, "high": 111.0, "close": 108.0, "shaved_head": False}
        advance_open(s, {("AAPL", "1d"): bar1}, cfg, today=date(2024, 1, 4))
        pt = s.get(PaperTrade, pt.id)
        assert pt.partial_done is True and pt.remaining_frac == pytest.approx(0.67)

        # bar 2: momentum flip with close 105.0 (above breakeven stop 101.0)
        bar2 = {"low": 102.0, "high": 107.0, "close": 105.0, "shaved_head": True}
        advance_open(s, {("AAPL", "1d"): bar2}, cfg, today=date(2024, 1, 5))
        closed = s.get(PaperTrade, pt.id)
        assert closed.status == "closed" and closed.exit_reason == "momentum_flip"
        assert closed.exit_price == 105.0
        partial_r = (110.0 - 101.0) / 7.0
        final_r = (105.0 - 101.0) / 7.0
        assert closed.realized_r == pytest.approx(0.33 * partial_r + 0.67 * final_r)


def test_collision_stop_and_target_same_bar_stops_out_no_partial():
    # Feature ON, but a single bar breaches BOTH the stop (low 93 <= 94) and the
    # target (high 111 >= 110). evaluate_exit ranks stop highest, so the reason is
    # "stop" -- the partial interception (which only fires on a "target" reason)
    # must NOT trigger: the trade goes terminal at -1R with partial_done False.
    cfg = StrategyConfig(partial_frac=0.33)
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        bar = {"low": 93.0, "high": 111.0, "close": 95.0, "shaved_head": False}
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.status == "closed" and closed.exit_reason == "stop"
        assert closed.exit_price == 94.0
        assert closed.partial_done is False
        assert closed.remaining_frac == 1.0
        assert closed.realized_r == (94.0 - 101.0) / 7.0   # full -1R, no partial leg


def test_collision_flip_and_target_same_bar_flips_no_partial():
    # Feature ON, but a single bar both flips (shaved_head) and pierces the target
    # (high 111 >= 110). momentum_flip outranks target, so the reason is
    # "momentum_flip" and no partial is booked: the trade exits whole at the close.
    cfg = StrategyConfig(partial_frac=0.33)
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        bar = {"low": 100.0, "high": 111.0, "close": 106.0, "shaved_head": True}
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.status == "closed" and closed.exit_reason == "momentum_flip"
        assert closed.exit_price == 106.0
        assert closed.partial_done is False
        assert closed.remaining_frac == 1.0
        assert closed.realized_r == (106.0 - 101.0) / 7.0   # full runner R, no partial leg


# --- parallel-arm dual-book + conditional partial (Step C) ------------------

ARMS = ("baseline", "partial33_cond")


def _open_dual(s):
    """Open the default candidate under both arms (baseline + conditional partial)."""
    open_from_signals(s, [_cand()], {("AAPL", "1d"): (105.0, 97.0)},
                      fill_date=date(2024, 1, 3), arms=ARMS)
    return repo.load_open_paper_trades(s)


def test_open_defaults_to_baseline_arm():
    # the arms kwarg defaults to ("baseline",), so existing single-book callers are
    # unchanged and every trade is tagged.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        _open_default(s)
        opened = repo.load_open_paper_trades(s)
        assert len(opened) == 1 and opened[0].arm == "baseline"


def test_dual_book_opens_one_trade_per_arm():
    # every fill is duplicated once per arm with identical entry economics.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        opened = _open_dual(s)
        assert len(opened) == 2
        assert {t.arm for t in opened} == set(ARMS)
        for t in opened:
            assert t.entry_price == 101.0 and t.risk == 7.0 and t.target == 110.0


def test_dual_book_arms_diverge_on_same_bar():
    # Same fill, same bar: the baseline arm books the full all-or-nothing target exit
    # while the conditional-partial arm scales out and keeps the runner. That clean
    # divergence on identical inputs is the whole point of the dual-book.
    arms = build_arms(StrategyConfig())
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        _open_dual(s)
        # target touched; softening (no shaved_bottom) so the partial arm scales
        bar = {"low": 100.0, "high": 111.0, "close": 108.0,
               "shaved_head": False, "shaved_bottom": False}
        advance_open(s, {("AAPL", "1d"): bar}, arms, today=date(2024, 1, 4))
        by_arm = {t.arm: t for t in _all_paper_trades(s)}

        base_t = by_arm["baseline"]
        assert base_t.status == "closed" and base_t.exit_reason == "target"
        assert base_t.realized_r == (110.0 - 101.0) / 7.0

        part_t = by_arm["partial33_cond"]
        assert part_t.status == "open" and part_t.partial_done is True
        assert part_t.remaining_frac == pytest.approx(0.67)
        assert part_t.stop == 101.0   # breakeven after the partial


def test_breakdown_by_arm_gives_per_arm_books():
    # breakdown(trades, "arm") reads the books back as a same-sample A/B.
    arms = build_arms(StrategyConfig())
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        _open_dual(s)
        bar1 = {"low": 100.0, "high": 111.0, "close": 108.0,
                "shaved_head": False, "shaved_bottom": False}
        advance_open(s, {("AAPL", "1d"): bar1}, arms, today=date(2024, 1, 4))
        # next bar dips to the partial arm's breakeven stop -> its runner closes too
        bar2 = {"low": 100.0, "high": 103.0, "close": 102.0,
                "shaved_head": False, "shaved_bottom": False}
        advance_open(s, {("AAPL", "1d"): bar2}, arms, today=date(2024, 1, 5))

        groups = breakdown(_all_paper_trades(s), "arm")
        assert set(groups) == {"baseline", "partial33_cond"}
        assert groups["baseline"].n_closed == 1
        assert groups["partial33_cond"].n_closed == 1


def test_advance_skips_trade_with_unknown_arm():
    # a trade whose arm is no longer in the roster is left open, not advanced under a
    # guessed config.
    cfg = StrategyConfig()
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        pt.arm = "ghost"
        s.commit()
        bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False}
        advance_open(s, {("AAPL", "1d"): bar}, {"baseline": cfg}, today=date(2024, 1, 4))
        held = s.get(PaperTrade, pt.id)
        assert held.status == "open" and held.hold_bars == 0


def test_conditional_partial_holds_full_on_strong_momentum():
    # require_softening + a STRONG bar at the target (shaved_bottom, body not
    # shrinking): suppress the target exit, HOLD the full position, leave the stop at
    # the ORIGINAL level (not breakeven), and re-evaluate next bar.
    cfg = StrategyConfig(partial_frac=0.33, partial_require_softening=True)
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        bar = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False,
               "shaved_bottom": True, "body_shrinking": False}
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        held = s.get(PaperTrade, pt.id)
        assert held.status == "open"
        assert held.partial_done is False
        assert held.stop == 94.0          # original stop, NOT breakeven
        assert held.hold_bars == 1
        assert held.exit_reason is None
        assert held.high_water == 111.0


def test_conditional_partial_scales_when_no_shaved_bottom():
    # softening via a lower wick (no shaved_bottom) -> scale out + breakeven.
    cfg = StrategyConfig(partial_frac=0.33, partial_require_softening=True)
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        bar = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False,
               "shaved_bottom": False, "body_shrinking": False}
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        held = s.get(PaperTrade, pt.id)
        assert held.partial_done is True
        assert held.remaining_frac == pytest.approx(0.67)
        assert held.stop == 101.0   # breakeven


def test_conditional_partial_scales_when_body_shrinking():
    # softening via a shrinking HA body even though there's no lower wick -> scale.
    cfg = StrategyConfig(partial_frac=0.33, partial_require_softening=True)
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        bar = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False,
               "shaved_bottom": True, "body_shrinking": True}
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        held = s.get(PaperTrade, pt.id)
        assert held.partial_done is True
        assert held.remaining_frac == pytest.approx(0.67)


def test_conditional_partial_scales_after_strength_fades():
    # strong at the target -> ride the full position; the NEXT bar is still above the
    # target but now softening -> scale out then.
    cfg = StrategyConfig(partial_frac=0.33, partial_require_softening=True)
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        strong = {"low": 100.0, "high": 111.0, "close": 110.5, "shaved_head": False,
                  "shaved_bottom": True, "body_shrinking": False}
        advance_open(s, {("AAPL", "1d"): strong}, cfg, today=date(2024, 1, 4))
        held = s.get(PaperTrade, pt.id)
        assert held.partial_done is False and held.hold_bars == 1 and held.stop == 94.0

        soft = {"low": 105.0, "high": 112.0, "close": 110.0, "shaved_head": False,
                "shaved_bottom": False, "body_shrinking": False}
        advance_open(s, {("AAPL", "1d"): soft}, cfg, today=date(2024, 1, 5))
        held = s.get(PaperTrade, pt.id)
        assert held.partial_done is True
        assert held.partial_price == 110.0
        assert held.remaining_frac == pytest.approx(0.67)
        assert held.stop == 101.0


def test_is_softening_predicate():
    assert _is_softening({"shaved_bottom": False, "body_shrinking": False}) is True
    assert _is_softening({"shaved_bottom": True, "body_shrinking": True}) is True
    assert _is_softening({"shaved_bottom": True, "body_shrinking": False}) is False
    assert _is_softening({}) is True   # missing fields default permissive


def test_build_arms_baseline_pinned_all_or_nothing():
    # baseline stays all-or-nothing even if the base config carries a stray partial.
    arms = build_arms(StrategyConfig(partial_frac=0.5))
    assert arms["baseline"].partial_frac == 0.0
    assert arms["partial33_cond"].partial_frac == 0.33
    assert arms["partial33_cond"].partial_require_softening is True


def test_high_water_tracks_highest_high_since_fill():
    # high_water starts >= entry and ratchets up with bar highs (never down).
    cfg = StrategyConfig()  # feature off; high_water tracking is unconditional
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        assert pt.high_water is None  # not yet advanced

        # a quiet up bar (high 103.0, no stop/flip/target) -> high_water = 103.0
        bar1 = {"low": 99.0, "high": 103.0, "close": 101.5, "shaved_head": False}
        advance_open(s, {("AAPL", "1d"): bar1}, cfg, today=date(2024, 1, 4))
        pt = s.get(PaperTrade, pt.id)
        assert pt.status == "open"
        assert pt.high_water == 103.0
        assert pt.high_water >= pt.entry_price

        # a lower-high bar must NOT lower the water mark
        bar2 = {"low": 99.0, "high": 102.0, "close": 100.5, "shaved_head": False}
        advance_open(s, {("AAPL", "1d"): bar2}, cfg, today=date(2024, 1, 5))
        pt = s.get(PaperTrade, pt.id)
        assert pt.high_water == 103.0  # held, not lowered

        # a new higher high lifts it
        bar3 = {"low": 100.0, "high": 106.0, "close": 104.0, "shaved_head": False}
        advance_open(s, {("AAPL", "1d"): bar3}, cfg, today=date(2024, 1, 6))
        pt = s.get(PaperTrade, pt.id)
        assert pt.high_water == 106.0

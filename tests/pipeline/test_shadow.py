from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import breakdown
from swing_screener.config import StrategyConfig
from swing_screener.db import repo
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import build_arms, build_stepping_arms
from swing_screener.pipeline.shadow import (
    FillCandidate,
    _is_softening,
    advance_open,
    open_from_signals,
    resolve_pending,
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


def test_same_trigger_is_not_rebooked_across_runs():
    """A weekly flip bar is re-detected as a 'prior signal' on EVERY daily run of the week
    (prior_frames drops only the partial current bucket) -- without trigger dedup each setup
    was booked 5-12 times, inflating the evidence base with correlated pseudo-samples."""
    engine = get_engine("sqlite:///:memory:")
    trig = datetime(2024, 1, 5, 16, 0, tzinfo=UTC)  # the completed weekly flip bar's timestamp
    cand = FillCandidate(ticker="AAPL", timeframe="1wk", horizon="long", signal_score=0.8,
                         rank=1, mtf_aligned=True, signal_id=None, zone=ZONE,
                         trigger_ts=trig)
    with Session(engine) as s:
        open_from_signals(s, [cand], {("AAPL", "1wk"): (105.0, 97.0)}, fill_date=date(2024, 1, 8))
        n_first = len(_all_paper_trades(s))
        assert n_first >= 1
        # next daily run re-detects the SAME completed bar -> must not re-book
        open_from_signals(s, [cand], {("AAPL", "1wk"): (106.0, 98.0)}, fill_date=date(2024, 1, 9))
        assert len(_all_paper_trades(s)) == n_first
        # a NEW trigger bar (next week's flip) books normally
        cand2 = FillCandidate(ticker="AAPL", timeframe="1wk", horizon="long", signal_score=0.8,
                              rank=1, mtf_aligned=True, signal_id=None, zone=ZONE,
                              trigger_ts=datetime(2024, 1, 12, 16, 0, tzinfo=UTC))
        open_from_signals(s, [cand2], {("AAPL", "1wk"): (106.0, 98.0)},
                          fill_date=date(2024, 1, 16))
        assert len(_all_paper_trades(s)) == 2 * n_first


def test_trigger_dedup_is_scoped_per_variant():
    """The same trigger booked under two screen VARIANTS is two separate books --
    dedup must never collapse the variant leaderboard."""
    engine = get_engine("sqlite:///:memory:")
    trig = datetime(2024, 1, 5, 16, 0, tzinfo=UTC)
    cand = FillCandidate(ticker="AAPL", timeframe="1wk", horizon="long", signal_score=0.8,
                         rank=1, mtf_aligned=True, signal_id=None, zone=ZONE,
                         trigger_ts=trig)
    with Session(engine) as s:
        open_from_signals(s, [cand], {("AAPL", "1wk"): (105.0, 97.0)},
                          fill_date=date(2024, 1, 8), variant="default")
        open_from_signals(s, [cand], {("AAPL", "1wk"): (105.0, 97.0)},
                          fill_date=date(2024, 1, 8), variant="tight_gate")
        variants = {t.variant for t in _all_paper_trades(s)}
        assert variants == {"default", "tight_gate"}


def test_tz_aware_trigger_ts_still_dedupes():
    """4h frames resample from tz-aware 1h data, so their trigger_ts arrives tz-AWARE --
    but the DB round-trip strips tzinfo, and a naive-vs-aware comparison never matches,
    silently disabling the dedup for exactly the timeframe with the most triggers. The
    booking path must normalize to naive so the second booking attempt is skipped."""
    from zoneinfo import ZoneInfo

    engine = get_engine("sqlite:///:memory:")
    aware = datetime(2024, 1, 5, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    cand = FillCandidate(ticker="AAPL", timeframe="4h", horizon="short", signal_score=0.8,
                         rank=1, mtf_aligned=True, signal_id=None, zone=ZONE,
                         trigger_ts=aware)
    with Session(engine) as s:
        open_from_signals(s, [cand], {("AAPL", "4h"): (105.0, 97.0)}, fill_date=date(2024, 1, 5))
        n_first = len(_all_paper_trades(s))
        open_from_signals(s, [cand], {("AAPL", "4h"): (106.0, 98.0)}, fill_date=date(2024, 1, 8))
        assert len(_all_paper_trades(s)) == n_first  # deduped despite the tz round-trip


def test_no_trigger_ts_books_every_time_legacy():
    """Candidates without a trigger timestamp (legacy/test callers) keep today's behavior."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_cand()], {("AAPL", "1d"): (120.0, 102.0)}, fill_date=date(2024, 1, 3))
        open_from_signals(s, [_cand()], {("AAPL", "1d"): (120.0, 102.0)}, fill_date=date(2024, 1, 4))
        assert len(_all_paper_trades(s)) == 2


def _wk_cand():
    return FillCandidate(ticker="AAPL", timeframe="1wk", horizon="long", signal_score=0.8,
                         rank=1, mtf_aligned=True, signal_id=None, zone=ZONE)


def test_weekly_trade_advances_once_per_completed_weekly_bar():
    """An open 1wk trade advances one bar per COMPLETED WEEKLY bar (keyed by the bar's
    label via bar_date), not once per daily run -- previously max_hold_bars['1wk']=8 fired
    after 8 trading days and momentum flips were read off half-formed weekly candles."""
    engine = get_engine("sqlite:///:memory:")
    quiet = {"low": 99.0, "high": 103.0, "close": 100.0, "shaved_head": False, "bearish": False}
    with Session(engine) as s:
        # filled Wednesday 2024-01-03 (mid-week)
        open_from_signals(s, [_wk_cand()], {("AAPL", "1wk"): (105.0, 97.0)},
                          fill_date=date(2024, 1, 3))
        # the fill week's own completed bar (Fri 01-05) must NOT advance the trade
        # (same bucket as the entry -- next-bar semantics).
        advance_open(s, {("AAPL", "1wk"): {**quiet, "bar_date": date(2024, 1, 5)}},
                     CFG, today=date(2024, 1, 8))
        assert repo.load_open_paper_trades(s)[0].hold_bars == 0
        # the NEXT completed weekly bar advances it exactly once...
        advance_open(s, {("AAPL", "1wk"): {**quiet, "bar_date": date(2024, 1, 12)}},
                     CFG, today=date(2024, 1, 15))
        assert repo.load_open_paper_trades(s)[0].hold_bars == 1
        # ...and re-presenting the SAME completed bar on later daily runs is a no-op.
        advance_open(s, {("AAPL", "1wk"): {**quiet, "bar_date": date(2024, 1, 12)}},
                     CFG, today=date(2024, 1, 16))
        advance_open(s, {("AAPL", "1wk"): {**quiet, "bar_date": date(2024, 1, 12)}},
                     CFG, today=date(2024, 1, 17))
        assert repo.load_open_paper_trades(s)[0].hold_bars == 1
        # a week later, the next completed bar advances again
        advance_open(s, {("AAPL", "1wk"): {**quiet, "bar_date": date(2024, 1, 19)}},
                     CFG, today=date(2024, 1, 22))
        assert repo.load_open_paper_trades(s)[0].hold_bars == 2


def test_stale_frame_never_advances_a_trade_on_its_own_fill_bar():
    """A ticker whose data is STALE (last bar label older than today) can still fill a
    trade on today's run; the same run's advance pass must not step it on that very bar
    -- a bar from before (or at) the entry is never a 'next bar'."""
    engine = get_engine("sqlite:///:memory:")
    quiet = {"low": 99.0, "high": 103.0, "close": 100.0, "shaved_head": False, "bearish": False}
    with Session(engine) as s:
        open_from_signals(s, [_cand()], {("AAPL", "1d"): (105.0, 97.0)},
                          fill_date=date(2024, 1, 5))  # filled on today's run...
        # ...but the frame's last completed bar is labeled three days earlier (stale data)
        advance_open(s, {("AAPL", "1d"): {**quiet, "bar_date": date(2024, 1, 2)}},
                     CFG, today=date(2024, 1, 5))
        assert repo.load_open_paper_trades(s)[0].hold_bars == 0  # not advanced


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


def _arm_cfgs(base=None):
    """An explicit two-arm mapping for the per-arm machinery tests.

    The live roster went baseline-only when the four exit arms settled futile
    (2026-08-15, edge/experiments.json), but ``advance_open``'s per-arm divergence is
    exactly what the NEXT arm will ride -- so these tests exercise it through a local
    mapping rather than through ``build_arms``, which would silently reduce them to
    single-book no-ops the moment the roster changed again.
    """
    base = base or StrategyConfig()
    return {
        "baseline": replace(base, partial_frac=0.0),
        "partial33_cond": replace(base, partial_frac=0.33, partial_require_softening=True),
    }


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
    arms = _arm_cfgs()
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
    arms = _arm_cfgs()
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


def test_draining_arm_trade_still_advances_to_a_close():
    """The retirement drain, end to end: a fill booked under a RETIRED arm is absent from
    the opening roster but present in the stepping set, so it keeps advancing and closes
    on its own policy instead of sitting open forever."""
    base = StrategyConfig()
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        pt.arm = "no_flip"          # retired 2026-08-15, still draining
        s.commit()
        assert "no_flip" not in build_arms(base)          # books no new fills
        assert "no_flip" in build_stepping_arms(base)     # but is still stepped

        bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False}
        advance_open(s, {("AAPL", "1d"): bar}, build_stepping_arms(base),
                     today=date(2024, 1, 4))
        closed = s.get(PaperTrade, pt.id)
        assert closed.status == "closed" and closed.exit_reason == "stop"
        assert closed.realized_r == (94.0 - 101.0) / 7.0


def test_draining_arm_honors_its_own_exit_policy_not_baselines():
    """no_flip exists to NOT close on a momentum flip. Draining it under the baseline
    config would close the trade here -- and quietly corrupt the settled book."""
    base = StrategyConfig()
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        pt.arm = "no_flip"
        s.commit()
        flip = {"low": 100.0, "high": 104.0, "close": 102.5, "shaved_head": True}
        advance_open(s, {("AAPL", "1d"): flip}, build_stepping_arms(base),
                     today=date(2024, 1, 4))
        held = s.get(PaperTrade, pt.id)
        assert held.status == "open" and held.exit_reason is None   # flip exit disabled
        assert held.hold_bars == 1                                  # but it DID advance


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
    # baseline stays all-or-nothing even if the base config carries a stray partial --
    # the control must not drift with the live config.
    arms = build_arms(StrategyConfig(partial_frac=0.5))
    assert arms["baseline"].partial_frac == 0.0


# --- Chandelier runner trail (Step D) ---------------------------------------

# fill_slippage_atr=0.0: these tests pin EXACT-level trail/partial mechanics; the
# haircut's own arithmetic is covered in test_shadow_slippage.py.
CHAND = StrategyConfig(partial_frac=0.33, partial_require_softening=True,
                       trail_mode="chandelier", chandelier_atr_mult=3.0,
                       fill_slippage_atr=0.0)


def _partial_then(s, cfg, *, atr=2.0):
    """Open the default trade and book the partial on bar 1 (softening target touch),
    returning the trade with partial_done=True, stop=breakeven 101, high_water=111."""
    pt = _open_default(s)
    bar1 = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False,
            "shaved_bottom": False, "atr": atr}
    advance_open(s, {("AAPL", "1d"): bar1}, cfg, today=date(2024, 1, 4))
    pt = s.get(PaperTrade, pt.id)
    assert pt.partial_done is True and pt.stop == 101.0 and pt.high_water == 111.0
    return pt


def test_chandelier_trail_ratchets_up_and_exits_on_the_trail():
    # After the partial, the stop ratchets up to prior high_water - 3*ATR each bar
    # (never down), and the runner exits when a pullback finally tags the trailed stop.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _partial_then(s, CHAND)   # stop 101, high_water 111

        # bar 2: high_water (prior) 111 -> trail 111 - 6 = 105; new high 120
        bar2 = {"low": 112.0, "high": 120.0, "close": 118.0, "shaved_head": False,
                "shaved_bottom": True, "atr": 2.0}
        advance_open(s, {("AAPL", "1d"): bar2}, CHAND, today=date(2024, 1, 5))
        pt = s.get(PaperTrade, pt.id)
        assert pt.status == "open" and pt.stop == 105.0 and pt.high_water == 120.0

        # bar 3: prior high_water 120 -> trail 120 - 6 = 114 (ratchets up from 105)
        bar3 = {"low": 119.0, "high": 121.0, "close": 120.0, "shaved_head": False,
                "shaved_bottom": True, "atr": 2.0}
        advance_open(s, {("AAPL", "1d"): bar3}, CHAND, today=date(2024, 1, 6))
        pt = s.get(PaperTrade, pt.id)
        assert pt.status == "open" and pt.stop == 114.0

        # bar 4: prior high_water 121 -> trail 115; the low tags it -> runner stops out
        bar4 = {"low": 113.0, "high": 121.0, "close": 114.0, "shaved_head": False,
                "shaved_bottom": True, "atr": 2.0}
        advance_open(s, {("AAPL", "1d"): bar4}, CHAND, today=date(2024, 1, 7))
        closed = s.get(PaperTrade, pt.id)
        assert closed.status == "closed" and closed.exit_reason == "stop"
        assert closed.exit_price == 115.0
        partial_r = (110.0 - 101.0) / 7.0
        final_r = (115.0 - 101.0) / 7.0
        assert closed.realized_r == pytest.approx(0.33 * partial_r + 0.67 * final_r)


def test_chandelier_trail_never_lowers_the_stop():
    # A widening ATR pushes the raw trail level below the current stop; the ratchet
    # (max with the existing stop) must hold the stop where it is.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _partial_then(s, CHAND)

        bar2 = {"low": 112.0, "high": 120.0, "close": 118.0, "shaved_head": False,
                "shaved_bottom": True, "atr": 2.0}
        advance_open(s, {("AAPL", "1d"): bar2}, CHAND, today=date(2024, 1, 5))
        assert s.get(PaperTrade, pt.id).stop == 105.0

        # ATR jumps to 10 -> raw trail = 120 - 30 = 90 < 105; stop must stay at 105
        bar3 = {"low": 112.0, "high": 121.0, "close": 119.0, "shaved_head": False,
                "shaved_bottom": True, "atr": 10.0}
        advance_open(s, {("AAPL", "1d"): bar3}, CHAND, today=date(2024, 1, 6))
        held = s.get(PaperTrade, pt.id)
        assert held.status == "open" and held.stop == 105.0


def test_chandelier_does_not_trail_before_a_partial():
    # Pre-partial the position keeps its hard stop -- the trail is a runner-only feature.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        # high 105 never reaches the target 110 -> no partial booked
        bar = {"low": 100.0, "high": 105.0, "close": 104.0, "shaved_head": False,
               "shaved_bottom": True, "atr": 2.0}
        advance_open(s, {("AAPL", "1d"): bar}, CHAND, today=date(2024, 1, 4))
        held = s.get(PaperTrade, pt.id)
        assert held.partial_done is False
        assert held.status == "open" and held.stop == 94.0   # original hard stop, untrailed


def test_breakeven_arm_keeps_static_stop_after_partial():
    # The incumbent runner (trail_mode="breakeven") ignores high_water/ATR: the stop
    # stays at breakeven no matter how far the runner extends.
    cfg = StrategyConfig(partial_frac=0.33, partial_require_softening=True)  # breakeven default
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _partial_then(s, cfg)
        bar2 = {"low": 112.0, "high": 120.0, "close": 118.0, "shaved_head": False,
                "shaved_bottom": True, "atr": 2.0}
        advance_open(s, {("AAPL", "1d"): bar2}, cfg, today=date(2024, 1, 5))
        held = s.get(PaperTrade, pt.id)
        assert held.status == "open" and held.stop == 101.0   # unchanged breakeven


def test_chandelier_trail_is_opt_in_not_a_live_default():
    # The Chandelier runner shipped as the partial33_chand ARM, retired futile on
    # 2026-08-15 (edge/experiments.json). The trail MACHINERY stays -- the CHAND tests
    # below exercise it -- but no roster arm turns it on any more, so the live book must
    # be untrailed. This is the regression guard on that retirement.
    assert build_arms(StrategyConfig())["baseline"].trail_mode == "breakeven"
    assert CHAND.trail_mode == "chandelier" and CHAND.chandelier_atr_mult == 3.0


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


# --- reversal engine shares the partial/trail machinery (Step E) -------------
#
# The shadow book is play-type-agnostic: reversal fills flow through the same
# open_from_signals + advance_open as continuation, so the conditional partial and
# the Chandelier trail apply to them automatically. These lock that in and prove the
# play_type tag survives, so the arm A/B can be sliced by engine.


def _rev_cand(ticker="AAPL"):
    return FillCandidate(ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8,
                         rank=1, mtf_aligned=False, signal_id=None, zone=ZONE,
                         play_type="reversal")


def test_reversal_missed_first_bar_pends_then_fills_in_window():
    """A reversal entry is a resting limit order: with a fill window, a first bar that
    gaps above the zone leaves the order PENDING (not terminal), and a later window bar
    that trades into the zone fills it at the worst-case in-zone price. The 2026-07 audit
    found ~95% of confirmed reversals unfillable in the old one-bar window."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_rev_cand()], {("AAPL", "1d"): (108.0, 102.0)},  # low > ceiling
                          fill_date=date(2024, 1, 3), reversal_fill_window_bars=3)
        pt = _all_paper_trades(s)[0]
        assert pt.fill_status == "pending" and pt.status == "pending"
        assert repo.load_open_paper_trades(s) == []  # a pending order is never bar-stepped
        # a later window bar trades into the zone -> filled at min(bar_high, ceiling)
        bar = {"low": 97.0, "high": 105.0, "close": 100.0, "bar_date": date(2024, 1, 4)}
        resolve_pending(s, {("AAPL", "1d"): bar}, window=3, today=date(2024, 1, 4))
        pt = _all_paper_trades(s)[0]
        assert pt.status == "open" and pt.fill_status == "filled"
        assert pt.entry_price == 101.0 and pt.entry_date == date(2024, 1, 4)
        assert pt.risk == 101.0 - 94.0 and pt.hold_bars == 0
        # the same run's advance pass must not step it on its own fill bar
        quiet = {"low": 99.0, "high": 103.0, "close": 100.0, "bar_date": date(2024, 1, 4)}
        advance_open(s, {("AAPL", "1d"): quiet}, CFG, today=date(2024, 1, 4))
        assert repo.load_open_paper_trades(s)[0].hold_bars == 0


def test_pending_expires_to_missed_after_window():
    engine = get_engine("sqlite:///:memory:")
    away = {"low": 102.0, "high": 108.0, "close": 105.0}  # never re-enters the zone
    with Session(engine) as s:
        open_from_signals(s, [_rev_cand()], {("AAPL", "1d"): (108.0, 102.0)},
                          fill_date=date(2024, 1, 3), reversal_fill_window_bars=2)
        resolve_pending(s, {("AAPL", "1d"): {**away, "bar_date": date(2024, 1, 4)}},
                        window=2, today=date(2024, 1, 4))
        pt = _all_paper_trades(s)[0]
        assert pt.fill_status == "missed" and pt.status == "closed"  # window exhausted


def test_pending_expiry_uses_each_variants_own_window():
    """Per-variant windows: a window-sweep variant's pending rows must expire under ITS
    config, not the base's. Booking already used the variant's width; expiry silently
    clamped every row to the single window passed to resolve_pending, corrupting any
    fill-window A/B (2026-07 review)."""
    engine = get_engine("sqlite:///:memory:")
    away = {"low": 102.0, "high": 108.0, "close": 105.0}  # never re-enters the zone
    with Session(engine) as s:
        open_from_signals(s, [_rev_cand()], {("AAPL", "1d"): (108.0, 102.0)},
                          fill_date=date(2024, 1, 3), variant="short_win",
                          reversal_fill_window_bars=2)
        open_from_signals(s, [_rev_cand()], {("AAPL", "1d"): (108.0, 102.0)},
                          fill_date=date(2024, 1, 3), variant="long_win",
                          reversal_fill_window_bars=4)
        windows = {"short_win": 2, "long_win": 4}
        resolve_pending(s, {("AAPL", "1d"): {**away, "bar_date": date(2024, 1, 4)}},
                        window=windows, today=date(2024, 1, 4))
        by_variant = {pt.variant: pt for pt in _all_paper_trades(s)}
        assert by_variant["short_win"].fill_status == "missed"    # its 2-bar window is up
        assert by_variant["long_win"].status == "pending"         # its 4-bar window is not
        # an unmapped variant falls back to the default variant's width, else the max
        resolve_pending(s, {("AAPL", "1d"): {**away, "bar_date": date(2024, 1, 5)}},
                        window=windows, today=date(2024, 1, 5))
        assert _all_paper_trades(s) is not None  # smoke: mapping path never raises


def test_pending_invalidated_when_stop_breaks_before_fill():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_rev_cand()], {("AAPL", "1d"): (108.0, 102.0)},
                          fill_date=date(2024, 1, 3), reversal_fill_window_bars=5)
        # the bar collapses through the stop without ever entering the zone
        crash = {"low": 90.0, "high": 93.0, "close": 91.0, "bar_date": date(2024, 1, 4)}
        resolve_pending(s, {("AAPL", "1d"): crash}, window=5, today=date(2024, 1, 4))
        pt = _all_paper_trades(s)[0]
        assert pt.fill_status == "invalidated" and pt.status == "closed"


def test_pending_step_is_idempotent_per_bar_label():
    engine = get_engine("sqlite:///:memory:")
    away = {"low": 102.0, "high": 108.0, "close": 105.0}
    with Session(engine) as s:
        open_from_signals(s, [_rev_cand()], {("AAPL", "1d"): (108.0, 102.0)},
                          fill_date=date(2024, 1, 3), reversal_fill_window_bars=5)
        bar = {**away, "bar_date": date(2024, 1, 4)}
        resolve_pending(s, {("AAPL", "1d"): bar}, window=5, today=date(2024, 1, 4))
        resolve_pending(s, {("AAPL", "1d"): bar}, window=5, today=date(2024, 1, 5))  # re-run
        pt = _all_paper_trades(s)[0]
        assert pt.status == "pending" and pt.pending_bars == 2  # 1 (booking) + 1, not 3


def test_continuation_missed_stays_terminal_even_with_window():
    """The window is a REVERSAL entry semantic (a limit resting into a pullback);
    continuation misses stay terminal -- their fill rate was never the problem."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_cand()], {("AAPL", "1d"): (120.0, 102.0)},
                          fill_date=date(2024, 1, 3), reversal_fill_window_bars=5)
        pt = _all_paper_trades(s)[0]
        assert pt.fill_status == "missed" and pt.status == "closed"


def test_reversal_trade_scales_out_at_its_target():
    # A reversal-tagged fill scales out at its (Fib) target via the shared machinery,
    # and the play_type tag is preserved through the scale-out.
    # zero haircut: this test pins the exact-level scale-out price (see CHAND note).
    cfg = StrategyConfig(partial_frac=0.33, partial_require_softening=True,
                         fill_slippage_atr=0.0)
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_rev_cand()], {("AAPL", "1d"): (105.0, 97.0)},
                          fill_date=date(2024, 1, 3))
        pt = repo.load_open_paper_trades(s)[0]
        assert pt.play_type == "reversal" and pt.entry_price == 101.0 and pt.target == 110.0

        bar = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False,
               "shaved_bottom": False, "atr": 2.0}
        advance_open(s, {("AAPL", "1d"): bar}, cfg, today=date(2024, 1, 4))
        held = s.get(PaperTrade, pt.id)
        assert held.partial_done is True and held.partial_price == 110.0
        assert held.stop == 101.0            # breakeven, same as continuation
        assert held.play_type == "reversal"  # tag preserved for slicing


def test_reversal_trade_trails_under_chandelier_arm():
    # The Chandelier runner trail applies to a reversal trade too.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_rev_cand()], {("AAPL", "1d"): (105.0, 97.0)},
                          fill_date=date(2024, 1, 3))
        pt = repo.load_open_paper_trades(s)[0]
        # bar 1: softening target touch -> partial (stop -> breakeven 101)
        bar1 = {"low": 100.0, "high": 111.0, "close": 109.0, "shaved_head": False,
                "shaved_bottom": False, "atr": 2.0}
        advance_open(s, {("AAPL", "1d"): bar1}, CHAND, today=date(2024, 1, 4))
        # bar 2: prior high_water 111 -> trail 111 - 6 = 105
        bar2 = {"low": 112.0, "high": 120.0, "close": 118.0, "shaved_head": False,
                "shaved_bottom": True, "atr": 2.0}
        advance_open(s, {("AAPL", "1d"): bar2}, CHAND, today=date(2024, 1, 5))
        held = s.get(PaperTrade, pt.id)
        assert held.play_type == "reversal" and held.stop == 105.0


def test_arm_ab_is_sliceable_by_play_type():
    # Every arm holds a reversal book, so filtering by play_type then breakdown(.,"arm")
    # gives a reversal-only A/B (the Step E measurement slice).
    arms = _arm_cfgs()
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        open_from_signals(s, [_rev_cand()], {("AAPL", "1d"): (105.0, 97.0)},
                          fill_date=date(2024, 1, 3), arms=tuple(arms))
        bar = {"low": 100.0, "high": 111.0, "close": 108.0, "shaved_head": False,
               "shaved_bottom": False, "atr": 2.0}
        advance_open(s, {("AAPL", "1d"): bar}, arms, today=date(2024, 1, 4))
        rev_trades = [t for t in _all_paper_trades(s) if t.play_type == "reversal"]
        groups = breakdown(rev_trades, "arm")
        assert set(groups) == set(arms)


def test_would_surface_stamp_is_persisted_from_the_candidate():
    """The booking-time surfacing estimate rides the candidate into the PaperTrade row --
    the reflection loop grades only stamped-True rows as gold (North Star #7)."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        surfaced = FillCandidate(ticker="AAA", timeframe="1d", horizon="medium",
                                 signal_score=0.9, rank=1, mtf_aligned=False, signal_id=None,
                                 zone=ZONE, would_surface=True)
        hidden = FillCandidate(ticker="BBB", timeframe="1d", horizon="medium",
                               signal_score=0.2, rank=9, mtf_aligned=False, signal_id=None,
                               zone=ZONE, would_surface=False)
        legacy = FillCandidate(ticker="CCC", timeframe="1d", horizon="medium",
                               signal_score=0.5, rank=2, mtf_aligned=False, signal_id=None,
                               zone=ZONE)  # unstamped (replay/tests) -> None
        bars = {("AAA", "1d"): (108.0, 100.0), ("BBB", "1d"): (108.0, 100.0),
                ("CCC", "1d"): (108.0, 100.0)}
        open_from_signals(s, [surfaced, hidden, legacy], bars, fill_date=date(2024, 1, 3))
        by_ticker = {t.ticker: t for t in _all_paper_trades(s)}
    assert by_ticker["AAA"].would_surface is True
    assert by_ticker["BBB"].would_surface is False
    assert by_ticker["CCC"].would_surface is None


def test_breakeven_ratchet_moves_the_stop_off_the_prior_bars_high_water(tmp_path):
    """The be_1r arm: once the PRIOR bar's high-water clears entry + 1R, the stop rises
    to breakeven -- never off this bar's own high (no intra-bar lookahead), never down."""
    from dataclasses import replace as dc_replace

    cfg = dc_replace(StrategyConfig(), breakeven_after_r=1.0)  # entry 101, risk 7 -> arm at 108
    bar1 = {"low": 100.0, "high": 108.5, "close": 107.0, "shaved_head": False}
    bar2 = {"low": 103.0, "high": 106.0, "close": 105.0, "shaved_head": False}
    with Session(get_engine(f"sqlite:///{tmp_path / 'be.sqlite'}")) as s:
        pt = _open_default(s)
        # bar 1 reaches entry+1R intra-bar: PRIOR high-water is still the entry, so the
        # ratchet must NOT fire off this bar's own high
        advance_open(s, {("AAPL", "1d"): bar1}, cfg, today=date(2024, 1, 4))
        pt = s.get(PaperTrade, pt.id)
        assert pt.stop == 94.0                        # unchanged: no lookahead
        # bar 2: the prior high-water (108.5) now clears 108 -> stop ratchets to entry
        advance_open(s, {("AAPL", "1d"): bar2}, cfg, today=date(2024, 1, 5))
        pt = s.get(PaperTrade, pt.id)
        assert pt.stop == 101.0                       # breakeven, never down from here
    # baseline config (knob off) never moves the stop pre-partial -- separate db
    with Session(get_engine(f"sqlite:///{tmp_path / 'base.sqlite'}")) as s2:
        pt2 = _open_default(s2)
        advance_open(s2, {("AAPL", "1d"): bar1}, StrategyConfig(), today=date(2024, 1, 4))
        advance_open(s2, {("AAPL", "1d"): bar2}, StrategyConfig(), today=date(2024, 1, 5))
        assert s2.get(PaperTrade, pt2.id).stop == 94.0


def test_low_water_tracks_the_lowest_low_since_fill():
    cfg = StrategyConfig()
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        pt = _open_default(s)
        assert pt.low_water is None
        advance_open(s, {("AAPL", "1d"): {"low": 99.0, "high": 103.0, "close": 101.0,
                                          "shaved_head": False}}, cfg,
                     today=date(2024, 1, 4))
        assert s.get(PaperTrade, pt.id).low_water == 99.0
        advance_open(s, {("AAPL", "1d"): {"low": 100.5, "high": 104.0, "close": 103.0,
                                          "shaved_head": False}}, cfg,
                     today=date(2024, 1, 5))
        assert s.get(PaperTrade, pt.id).low_water == 99.0  # never rises

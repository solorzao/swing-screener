"""The learning join: score unscored AnalystCalls against the shadow book.

Each AnalystCall is logged on a run's pick ``(ticker, timeframe, play_type, run_date)``.
The matching shadow outcome is the BASELINE/DEFAULT_VARIANT paper trade that FILLED on
the first run AFTER the call (``opened_date`` earliest > ``run_date``), once it closes
with a realized R. ``score_analyst_calls`` stamps that R + a ``scored_at`` onto the call;
picks that haven't filled+closed yet stay unscored (rescored on a later run).
"""

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import AnalystCall, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.variants import DEFAULT_VARIANT

_D = date(2026, 6, 19)  # the call's run_date everywhere below


def _call(*, ticker="AMD", timeframe="1d", play_type="continuation", run_date=_D,
          realized_r=None, scored_at=None) -> AnalystCall:
    return AnalystCall(
        created_date=run_date, ticker=ticker, timeframe=timeframe, play_type=play_type,
        run_date=run_date, baseline_conviction="medium", final_conviction="high",
        nudge_reason="x", model="claude-opus-4-8",
        realized_r=realized_r, scored_at=scored_at,
    )


def _pt(*, ticker="AMD", timeframe="1d", play_type="continuation", arm=BASELINE,
        variant=DEFAULT_VARIANT, status="closed", fill_status="filled", realized_r=1.5,
        opened_date=date(2026, 6, 20), exit_date=date(2026, 6, 25)) -> PaperTrade:
    return PaperTrade(
        ticker=ticker, timeframe=timeframe, horizon="medium", play_type=play_type,
        arm=arm, variant=variant, signal_score=0.8, rank=1, fill_status=fill_status,
        stop=95.0, target=110.0, risk=5.0, status=status, realized_r=realized_r,
        opened_date=opened_date, exit_date=exit_date,
    )


def test_scores_call_from_first_post_call_closed_baseline_default_trade() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_call())
        s.add(_pt(realized_r=1.5, opened_date=date(2026, 6, 20), exit_date=date(2026, 6, 25)))
        s.commit()

        scored = repo.score_analyst_calls(s)
        assert scored == 1
        call = s.query(AnalystCall).one()
        assert call.realized_r == 1.5
        assert call.scored_at == date(2026, 6, 25)


def test_call_is_never_scored_by_a_distant_unrelated_trade() -> None:
    """The join is bounded in time: a call whose own pick never filled must NOT be graded
    by whatever same-key trade happens to close weeks later — that's a different setup at
    different levels, and miscored calls poison the calibration record the autonomy gate
    reads (2026-07 audit). The pick's own booking lands within days of the call (next run
    for 1d/4h; the period-end run + weekend for 1wk), so anything beyond the window is
    someone else's trade."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_call())  # run_date 2026-06-19
        # same pick keys, but opened 3 weeks after the call -> a different setup entirely
        s.add(_pt(realized_r=-2.0, opened_date=date(2026, 7, 10), exit_date=date(2026, 7, 15)))
        s.commit()

        assert repo.score_analyst_calls(s) == 0
        call = s.query(AnalystCall).one()
        assert call.realized_r is None and call.scored_at is None


def test_call_scored_by_a_late_but_in_window_booking() -> None:
    """A 1wk pick called mid-week books on the period-end run, up to ~a week after the
    call (plus a weekend) — that booking is still the call's own setup and must score."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_call(timeframe="1wk"))  # run_date Fri-adjacent 2026-06-19
        s.add(_pt(timeframe="1wk", realized_r=0.8,
                  opened_date=date(2026, 6, 26), exit_date=date(2026, 7, 24)))  # 7 days later
        s.commit()

        assert repo.score_analyst_calls(s) == 1
        assert s.query(AnalystCall).one().realized_r == 0.8


def test_unfilled_pick_stays_unscored() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_call())
        # only an OPEN trade exists for this pick -> nothing to score yet.
        s.add(_pt(status="open", realized_r=None, exit_date=None))
        s.commit()

        scored = repo.score_analyst_calls(s)
        assert scored == 0
        call = s.query(AnalystCall).one()
        assert call.realized_r is None
        assert call.scored_at is None


def test_picks_earliest_opened_after_run_date() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_call())
        # two closed trades for the same pick: d+1 and d+5. The d+1 one is the fill of
        # the call's prior-bar signal; it must win even though both qualify.
        s.add(_pt(realized_r=2.0, opened_date=date(2026, 6, 20), exit_date=date(2026, 6, 24)))
        s.add(_pt(realized_r=-1.0, opened_date=date(2026, 6, 24), exit_date=date(2026, 6, 30)))
        s.commit()

        scored = repo.score_analyst_calls(s)
        assert scored == 1
        call = s.query(AnalystCall).one()
        assert call.realized_r == 2.0
        assert call.scored_at == date(2026, 6, 24)


def test_trade_opened_on_or_before_run_date_is_not_matched() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_call(run_date=_D))
        # opened ON the run_date (not strictly after) -> not the post-call fill.
        s.add(_pt(realized_r=1.5, opened_date=_D, exit_date=date(2026, 6, 24)))
        s.commit()

        assert repo.score_analyst_calls(s) == 0
        assert s.query(AnalystCall).one().realized_r is None


def test_wrong_arm_variant_open_and_missed_are_not_matched() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_call())
        s.add(_pt(arm="partial33_cond"))                       # wrong arm
        s.add(_pt(variant="extguard_tight"))                   # wrong variant
        s.add(_pt(status="open", realized_r=None, exit_date=None))  # not closed
        s.add(_pt(fill_status="missed", realized_r=None, status="closed"))  # missed
        s.add(_pt(realized_r=None))                            # closed+filled but no R
        s.commit()

        assert repo.score_analyst_calls(s) == 0
        assert s.query(AnalystCall).one().realized_r is None


def test_wrong_pick_keys_are_not_matched() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_call(ticker="AMD", timeframe="1d", play_type="continuation"))
        s.add(_pt(ticker="NVDA"))                  # wrong ticker
        s.add(_pt(timeframe="1wk"))                # wrong timeframe
        s.add(_pt(play_type="reversal"))           # wrong play_type
        s.commit()

        assert repo.score_analyst_calls(s) == 0
        assert s.query(AnalystCall).one().realized_r is None


def test_idempotent_second_run_rescores_nothing() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_call())
        s.add(_pt(realized_r=1.5, opened_date=date(2026, 6, 20), exit_date=date(2026, 6, 25)))
        s.commit()

        assert repo.score_analyst_calls(s) == 1
        # a second pass finds the call already scored (scored_at set) -> no re-score.
        assert repo.score_analyst_calls(s) == 0
        call = s.query(AnalystCall).one()
        assert call.realized_r == 1.5
        assert call.scored_at == date(2026, 6, 25)


def test_scored_at_falls_back_to_today_when_trade_has_no_exit_date() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_call())
        # a closed+filled trade with a realized R but no exit_date (defensive): the call
        # is still scored, stamping scored_at to today's date.
        s.add(_pt(realized_r=1.5, opened_date=date(2026, 6, 20), exit_date=None))
        s.commit()

        assert repo.score_analyst_calls(s) == 1
        call = s.query(AnalystCall).one()
        assert call.realized_r == 1.5
        assert call.scored_at == datetime.now(UTC).date()

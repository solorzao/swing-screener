from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade, OptionSetup
from swing_screener.db.session import get_engine
from swing_screener.options.checklist import CHECKLIST_ITEMS
from swing_screener.options.journal import (
    SettledTradeError,
    create_setup,
    list_recent_setups,
    list_setups,
    set_status,
)


def _all_true() -> dict[str, bool]:
    return {i.key: True for i in CHECKLIST_ITEMS}


def test_create_setup_computes_grade_and_stores_items() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(
            s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY", direction="long",
            checklist=_all_true(), entry=558.0, stop=556.5, target=565.0,
            regime="positive", pivot_level=557.5, pattern="flag", notes="",
        )
        assert row.grade == "A+"
        assert row.chk_confirmation_candle is True
        assert row.status == "idea"


def test_taking_a_setup_opens_a_paper_trade() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                           direction="long", checklist=_all_true(),
                           entry=558.0, stop=556.5, target=565.0)
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 6, tzinfo=UTC))
        trades = list(s.scalars(select(OptionPaperTrade)))
        assert len(trades) == 1
        assert trades[0].setup_id == row.id
        assert trades[0].account == "options-lab" and trades[0].status == "open"


def test_skip_does_not_open_a_trade_and_double_take_is_noop() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                           direction="long", checklist=_all_true(),
                           entry=558.0, stop=556.5, target=565.0)
        set_status(s, row.id, "skipped", at=datetime(2026, 7, 13, 10, 6, tzinfo=UTC))
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 7, tzinfo=UTC))
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 8, tzinfo=UTC))
        assert len(list(s.scalars(select(OptionPaperTrade)))) == 1


def test_list_setups_filters_by_day() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                     direction="long", checklist=_all_true())
        create_setup(s, ts=datetime(2026, 7, 14, 10, 5, tzinfo=UTC), underlying="QQQ",
                     direction="long", checklist=_all_true())
        assert [x.underlying for x in list_setups(s, day=date(2026, 7, 13))] == ["SPY"]
        assert len(list_setups(s, day=None)) == 2


def test_untaking_deletes_the_open_paper_trade() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                           direction="long", checklist=_all_true(),
                           entry=558.0, stop=556.5, target=565.0)
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 6, tzinfo=UTC))
        set_status(s, row.id, "skipped", at=datetime(2026, 7, 13, 10, 7, tzinfo=UTC))
        assert list(s.scalars(select(OptionPaperTrade))) == []
        # re-take after the un-take opens a fresh trade
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 8, tzinfo=UTC))
        assert len(list(s.scalars(select(OptionPaperTrade)))) == 1


def test_untaking_a_settled_setup_is_refused() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                           direction="long", checklist=_all_true(),
                           entry=558.0, stop=556.5, target=565.0)
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 6, tzinfo=UTC))
        trade = s.scalars(select(OptionPaperTrade)).one()
        trade.status = "closed"
        trade.exit_reason = "target"
        trade.realized_r = 2.0
        s.commit()
        with pytest.raises(SettledTradeError):
            set_status(s, row.id, "skipped", at=datetime(2026, 7, 13, 16, 0, tzinfo=UTC))
        s.rollback()
        # the settled trade and the taken status both survive the refusal
        assert s.scalars(select(OptionPaperTrade)).one().status == "closed"
        assert s.get(OptionSetup, row.id).status == "taken"


def test_list_recent_setups_is_a_bounded_newest_first_window() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        create_setup(s, ts=datetime(2026, 7, 3, 10, 5, tzinfo=UTC), underlying="OLD",
                     direction="long", checklist=_all_true())
        create_setup(s, ts=datetime(2026, 7, 11, 10, 5, tzinfo=UTC), underlying="EDGE",
                     direction="long", checklist=_all_true())
        create_setup(s, ts=datetime(2026, 7, 17, 10, 5, tzinfo=UTC), underlying="NEW",
                     direction="long", checklist=_all_true())
        recent = list_recent_setups(s, end_day=date(2026, 7, 17))
        # 7 calendar days ending 07-17 -> [07-11, 07-17]; 07-03 falls out
        assert [x.underlying for x in recent] == ["NEW", "EDGE"]


# --- T2: play_type + autograde provenance + R:R integrity -------------------

def test_create_setup_persists_play_type_and_autograde_json() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(
            s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY", direction="long",
            checklist=_all_true(), entry=558.0, stop=556.5, target=565.0,
            play_type="breakout", autograde_json='{"machine_verdict": "yes"}',
        )
        assert row.play_type == "breakout"
        assert row.autograde_json == '{"machine_verdict": "yes"}'
        # reload from the DB to prove they persisted, not just set in memory
        reloaded = s.get(OptionSetup, row.id)
        assert reloaded is not None
        assert reloaded.play_type == "breakout"
        assert reloaded.autograde_json == '{"machine_verdict": "yes"}'


def test_create_setup_defaults_play_type_empty_and_autograde_null() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                           direction="long", checklist=_all_true())
        assert row.play_type == ""        # legacy/unspecified
        assert row.autograde_json is None  # no auto-grade ran


def test_create_setup_rejects_rr_tick_that_contradicts_levels() -> None:
    # chk_rr_at_least_2 ticked True but entry/stop/target compute R:R 0.50 < 2 --
    # a stored lie. The write is refused BEFORE anything persists.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        with pytest.raises(ValueError, match="R:R"):
            create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                         direction="long", checklist=_all_true(),
                         entry=100.0, stop=98.0, target=101.0)  # risk 2, reward 1 -> 0.5
        assert list(s.scalars(select(OptionSetup))) == []  # nothing written


def test_create_setup_rr_contradiction_not_raised_when_levels_missing() -> None:
    # The tick stands when levels are absent -- the trader may work from a fuller plan.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                           direction="long", checklist=_all_true(),
                           entry=None, stop=None, target=None)
        assert row.chk_rr_at_least_2 is True


def test_create_setup_rr_contradiction_not_raised_when_tick_is_false() -> None:
    # Levels compute a poor R:R but the box is UNticked -- no contradiction to reject.
    checklist = _all_true()
    checklist["chk_rr_at_least_2"] = False
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                           direction="long", checklist=checklist,
                           entry=100.0, stop=98.0, target=101.0)
        assert row.chk_rr_at_least_2 is False


def test_create_setup_rr_boundary_ratio_exactly_min_passes() -> None:
    # ratio exactly rr_min (2.0) is NOT < rr_min -- the tick is honest, write proceeds.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                           direction="long", checklist=_all_true(),
                           entry=100.0, stop=99.0, target=102.0)  # risk 1, reward 2 -> 2.0
        assert row.chk_rr_at_least_2 is True
        assert row.grade == "A+"


def test_create_setup_rejects_rr_tick_on_side_insane_ordering() -> None:
    # A LONG with stop ABOVE entry computes abs-ratio 10 -- but _item_rr calls
    # these levels a FAIL (not stop<entry<target), so a ticked R:R box is a lie
    # the abs-ratio gate alone would store as A+. Ordering must gate first.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        with pytest.raises(ValueError, match="R:R"):
            create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                         direction="long", checklist=_all_true(),
                         entry=100.0, stop=110.0, target=200.0)
        assert list(s.scalars(select(OptionSetup))) == []  # nothing written


def test_create_setup_rejects_rr_tick_on_zero_risk() -> None:
    # entry == stop: R:R is undefined, which can never honestly claim >= 2. The
    # strict ordering check rejects it for either side (no inf escape hatch).
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        with pytest.raises(ValueError, match="R:R"):
            create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                         direction="long", checklist=_all_true(),
                         entry=100.0, stop=100.0, target=300.0)
        assert list(s.scalars(select(OptionSetup))) == []


def test_create_setup_proper_short_bracket_passes() -> None:
    # target < entry < stop with ratio 3.0 -- a side-sane short must not be
    # rejected by the long ordering rule.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5, tzinfo=UTC), underlying="SPY",
                           direction="short", checklist=_all_true(),
                           entry=100.0, stop=101.0, target=97.0)  # risk 1, reward 3
        assert row.chk_rr_at_least_2 is True
        assert row.grade == "A+"

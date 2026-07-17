from datetime import date, datetime

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
            s, ts=datetime(2026, 7, 13, 10, 5), underlying="SPY", direction="long",
            checklist=_all_true(), entry=558.0, stop=556.5, target=565.0,
            regime="positive", pivot_level=557.5, pattern="flag", notes="",
        )
        assert row.grade == "A+"
        assert row.chk_confirmation_candle is True
        assert row.status == "idea"


def test_taking_a_setup_opens_a_paper_trade() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5), underlying="SPY",
                           direction="long", checklist=_all_true(),
                           entry=558.0, stop=556.5, target=565.0)
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 6))
        trades = list(s.scalars(select(OptionPaperTrade)))
        assert len(trades) == 1
        assert trades[0].setup_id == row.id
        assert trades[0].account == "options-lab" and trades[0].status == "open"


def test_skip_does_not_open_a_trade_and_double_take_is_noop() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5), underlying="SPY",
                           direction="long", checklist=_all_true(),
                           entry=558.0, stop=556.5, target=565.0)
        set_status(s, row.id, "skipped", at=datetime(2026, 7, 13, 10, 6))
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 7))
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 8))
        assert len(list(s.scalars(select(OptionPaperTrade)))) == 1


def test_list_setups_filters_by_day() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        create_setup(s, ts=datetime(2026, 7, 13, 10, 5), underlying="SPY",
                     direction="long", checklist=_all_true())
        create_setup(s, ts=datetime(2026, 7, 14, 10, 5), underlying="QQQ",
                     direction="long", checklist=_all_true())
        assert [x.underlying for x in list_setups(s, day=date(2026, 7, 13))] == ["SPY"]
        assert len(list_setups(s, day=None)) == 2


def test_untaking_deletes_the_open_paper_trade() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5), underlying="SPY",
                           direction="long", checklist=_all_true(),
                           entry=558.0, stop=556.5, target=565.0)
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 6))
        set_status(s, row.id, "skipped", at=datetime(2026, 7, 13, 10, 7))
        assert list(s.scalars(select(OptionPaperTrade))) == []
        # re-take after the un-take opens a fresh trade
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 8))
        assert len(list(s.scalars(select(OptionPaperTrade)))) == 1


def test_untaking_a_settled_setup_is_refused() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = create_setup(s, ts=datetime(2026, 7, 13, 10, 5), underlying="SPY",
                           direction="long", checklist=_all_true(),
                           entry=558.0, stop=556.5, target=565.0)
        set_status(s, row.id, "taken", at=datetime(2026, 7, 13, 10, 6))
        trade = s.scalars(select(OptionPaperTrade)).one()
        trade.status = "closed"
        trade.exit_reason = "target"
        trade.realized_r = 2.0
        s.commit()
        with pytest.raises(SettledTradeError):
            set_status(s, row.id, "skipped", at=datetime(2026, 7, 13, 16, 0))
        s.rollback()
        # the settled trade and the taken status both survive the refusal
        assert s.scalars(select(OptionPaperTrade)).one().status == "closed"
        assert s.get(OptionSetup, row.id).status == "taken"


def test_list_recent_setups_is_a_bounded_newest_first_window() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        create_setup(s, ts=datetime(2026, 7, 3, 10, 5), underlying="OLD",
                     direction="long", checklist=_all_true())
        create_setup(s, ts=datetime(2026, 7, 11, 10, 5), underlying="EDGE",
                     direction="long", checklist=_all_true())
        create_setup(s, ts=datetime(2026, 7, 17, 10, 5), underlying="NEW",
                     direction="long", checklist=_all_true())
        recent = list_recent_setups(s, end_day=date(2026, 7, 17))
        # 7 calendar days ending 07-17 -> [07-11, 07-17]; 07-03 falls out
        assert [x.underlying for x in recent] == ["NEW", "EDGE"]

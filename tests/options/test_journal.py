from datetime import datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade
from swing_screener.db.session import get_engine
from swing_screener.options.checklist import CHECKLIST_ITEMS
from swing_screener.options.journal import create_setup, list_setups, set_status


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
    from sqlalchemy import select
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
    from sqlalchemy import select
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
    from datetime import date
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        create_setup(s, ts=datetime(2026, 7, 13, 10, 5), underlying="SPY",
                     direction="long", checklist=_all_true())
        create_setup(s, ts=datetime(2026, 7, 14, 10, 5), underlying="QQQ",
                     direction="long", checklist=_all_true())
        assert [x.underlying for x in list_setups(s, day=date(2026, 7, 13))] == ["SPY"]
        assert len(list_setups(s, day=None)) == 2

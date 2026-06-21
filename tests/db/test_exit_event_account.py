"""The ``account`` dimension on ``ExitEvent`` (display-only book isolation).

Phase 3 records the ENTIRE shadow book under ``is_paper=True`` -- both the research
grid AND the curated intent book (``account="paper"``) -- so ``is_paper`` alone can't
separate research-grid exits from intent-book paper exits. ``record_exit_event`` grows
an ``account`` keyword (default ``"research"``, mirroring ``PaperTrade.account``) so the
exit log can be sliced by book. Purely additive: the default keeps every existing caller
correct and backfills existing rows to the research grid.

In-memory SQLite, no network.
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import ExitEvent
from swing_screener.db.session import get_engine


def _session() -> Session:
    return Session(get_engine("sqlite:///:memory:"))


def test_record_exit_event_defaults_account_to_research() -> None:
    with _session() as s:
        ev = repo.record_exit_event(
            s, is_paper=True, trade_id=1, tier="hard", reason="stop",
            message="stopped out", created_date=date(2026, 6, 21),
        )
        assert ev.account == "research"
        # round-trips from the DB, not just the in-memory instance.
        fetched = s.get(ExitEvent, ev.id)
        assert fetched is not None and fetched.account == "research"


def test_record_exit_event_persists_explicit_paper_account() -> None:
    with _session() as s:
        ev = repo.record_exit_event(
            s, is_paper=True, trade_id=2, tier="hard", reason="target",
            message="took profit", created_date=date(2026, 6, 21), account="paper",
        )
        assert ev.account == "paper"
        fetched = s.get(ExitEvent, ev.id)
        assert fetched is not None and fetched.account == "paper"


def test_exit_event_account_column_is_bounded_for_azure_sql() -> None:
    # bounded String(16) (un-indexable NVARCHAR(max) would break Azure SQL),
    # mirroring PaperTrade.account.
    assert ExitEvent.__table__.c.account.type.length == 16

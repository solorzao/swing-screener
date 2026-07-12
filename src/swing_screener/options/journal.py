"""The setup journal: create graded setups, transition their status, and open
a paper trade the moment a setup is taken.

Repo-style plain functions (session first, commit at the end). A setup is graded
at creation via the checklist ``grade()`` -- which raises on unknown/missing keys,
so a malformed checklist never persists a mis-graded row. Taking a setup opens
exactly one ``OptionPaperTrade`` on the firewalled ``options-lab`` book; a
double-take is a no-op (the guard queries for an existing trade first).
"""

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade, OptionSetup
from swing_screener.options.checklist import grade

_STATUSES = frozenset({"idea", "taken", "skipped"})


def create_setup(
    session: Session,
    *,
    ts: datetime,
    underlying: str,
    direction: str,
    checklist: dict[str, bool],
    entry: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    regime: str = "unknown",
    pivot_level: float | None = None,
    pattern: str = "",
    notes: str = "",
    gex_snapshot_id: int | None = None,
) -> OptionSetup:
    """Journal a new setup, graded against the 12-point checklist at decision time.

    ``checklist`` must carry every ``chk_*`` key and only known keys -- ``grade()``
    raises ``KeyError`` otherwise, before anything is written.
    """
    setup_grade = grade(checklist)
    setup = OptionSetup(
        ts=ts,
        underlying=underlying,
        direction=direction,
        gex_snapshot_id=gex_snapshot_id,
        regime=regime,
        pivot_level=pivot_level,
        pattern=pattern,
        entry=entry,
        stop=stop,
        target=target,
        grade=setup_grade,
        status="idea",
        notes=notes,
        **checklist,
    )
    session.add(setup)
    session.commit()
    session.refresh(setup)
    return setup


def set_status(
    session: Session, setup_id: int, status: str, *, at: datetime
) -> OptionSetup:
    """Transition a setup's status; opening a paper trade on the way TO ``taken``.

    The paper trade is opened once -- a repeated take (or a take after a skip)
    never opens a second trade, since we query for an existing one first.
    """
    if status not in _STATUSES:
        raise ValueError(f"unknown setup status: {status!r}")
    setup = session.get(OptionSetup, setup_id)
    if setup is None:
        raise ValueError(f"no setup with id {setup_id}")

    if status == "taken":
        existing = session.scalar(
            select(OptionPaperTrade).where(OptionPaperTrade.setup_id == setup_id)
        )
        if existing is None:
            session.add(
                OptionPaperTrade(
                    setup_id=setup.id,
                    account="options-lab",
                    strategy="gex",
                    underlying=setup.underlying,
                    direction=setup.direction,
                    opened_at=at,
                    entry=setup.entry,
                    stop=setup.stop,
                    target=setup.target,
                    status="open",
                )
            )

    setup.status = status
    session.commit()
    session.refresh(setup)
    return setup


def list_setups(session: Session, *, day: date | None) -> list[OptionSetup]:
    """Setups newest-first; when ``day`` is given, only those with ``ts`` in
    ``[day 00:00, day+1)``."""
    stmt = select(OptionSetup).order_by(OptionSetup.ts.desc())
    if day is not None:
        start = datetime(day.year, day.month, day.day)
        stmt = stmt.where(
            OptionSetup.ts >= start,
            OptionSetup.ts < start + timedelta(days=1),
        )
    return list(session.scalars(stmt))

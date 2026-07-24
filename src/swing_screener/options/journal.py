"""The setup journal: create graded setups, transition their status, and open
a paper trade the moment a setup is taken.

Repo-style plain functions (session first, commit at the end). A setup is graded
at creation via the checklist ``grade()`` -- which raises on unknown/missing keys,
so a malformed checklist never persists a mis-graded row. Taking a setup opens
exactly one ``OptionPaperTrade`` on the firewalled ``options-lab`` book; a
double-take is a no-op (the guard queries for an existing trade first).

LAB TIME CONVENTION: every lab writer stamps NAIVE US/Eastern wall time
(``chain._now_eastern``). ``settle.py`` depends on it -- bar indexes are
normalized to naive Eastern and compared against ``opened_at`` directly -- so
``at``/``ts`` arguments must never be UTC or machine-local.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade, OptionSetup
from swing_screener.options.checklist import grade
from swing_screener.options.config import GexConfig

_STATUSES = frozenset({"idea", "taken", "skipped"})


class SettledTradeError(ValueError):
    """A status change would orphan a SETTLED paper trade: the setup was taken and
    its trade already closed into the lab book, so un-taking it would silently
    rewrite graded history. The cockpit router maps this to 409."""


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
    play_type: str = "",
    pivot_level: float | None = None,
    pattern: str = "",
    notes: str = "",
    gex_snapshot_id: int | None = None,
    autograde_json: str | None = None,
    rr_min: float = GexConfig().rr_min,
) -> OptionSetup:
    """Journal a new setup, graded against the 12-point checklist at decision time.

    ``checklist`` must carry every ``chk_*`` key and only known keys -- ``grade()``
    raises ``KeyError`` otherwise, before anything is written. ``autograde_json`` is
    the machine provenance stored opaquely (None when no auto-grade ran).

    Integrity: a ticked ``chk_rr_at_least_2`` that CONTRADICTS the typed levels is a
    stored lie, so it is refused with ``ValueError`` before any write -- but only on
    a demonstrable contradiction. The tick stands when the levels are missing (the
    trader may be working from a fuller plan); with entry, stop AND target all
    present the check mirrors ``autograde._item_rr`` exactly: side-sane ordering
    first (long: stop<entry<target; short: target<entry<stop), then the
    reward-to-risk floor ``rr_min`` (defaulting to the config value so the router
    can thread its own cfg). Ordering gates FIRST because the abs ratio alone
    lies for side-insane levels (a long with stop above entry "computes" 10:1),
    and strict ordering makes risk > 0, so the undefined zero-risk R:R
    (entry == stop) is rejected here too.
    """
    if (
        checklist.get("chk_rr_at_least_2")
        and entry is not None and stop is not None and target is not None
    ):
        # Non-"long" grades with short ordering -- the lab's is_long posture
        # (settle.py grades trades the same way); the message names the direction
        # so a mis-sent one reads back in the 422.
        ordered = stop < entry < target if direction == "long" else target < entry < stop
        if not ordered:
            raise ValueError(
                f"checklist claims R:R >= 2 but {direction} levels are not ordered "
                f"(stop {stop:.2f}, entry {entry:.2f}, target {target:.2f})"
            )
        ratio = abs(target - entry) / abs(entry - stop)  # ordered strictly -> risk > 0
        if ratio < rr_min:
            raise ValueError(
                f"checklist claims R:R >= 2 but levels compute R:R {ratio:.2f}"
            )
    setup_grade = grade(checklist)
    setup = OptionSetup(
        ts=ts,
        underlying=underlying,
        direction=direction,
        play_type=play_type,
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
        autograde_json=autograde_json,
        **checklist,
    )
    session.add(setup)
    session.commit()
    session.refresh(setup)
    return setup


def set_status(
    session: Session, setup_id: int, status: str, *, at: datetime
) -> OptionSetup:
    """Transition a setup's status; opening a paper trade on the way TO ``taken``
    and deleting the not-yet-settled trade on the way OFF it.

    The paper trade is opened once -- a repeated take (or a take after a skip)
    never opens a second trade, since we query for an existing one first.
    Leaving ``taken`` (-> idea/skipped) deletes the linked OPEN trade, so the
    settle sweep never grades a setup that was un-taken; if the trade already
    SETTLED (closed), the transition is refused with ``SettledTradeError`` --
    graded history is immutable. ``at`` follows the lab's naive-Eastern
    convention (module docstring).
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
    elif setup.status == "taken":
        linked = session.scalar(
            select(OptionPaperTrade).where(OptionPaperTrade.setup_id == setup_id)
        )
        if linked is not None:
            if linked.status == "closed":
                raise SettledTradeError(
                    f"setup {setup_id} already settled; cannot leave 'taken'"
                )
            session.delete(linked)

    setup.status = status
    session.commit()
    session.refresh(setup)
    return setup


def list_setups(session: Session, *, day: date | None) -> list[OptionSetup]:
    """Setups newest-first; when ``day`` is given, only those with ``ts`` in
    ``[day 00:00, day+1)``."""
    stmt = select(OptionSetup).order_by(OptionSetup.ts.desc())
    if day is not None:
        start = datetime(day.year, day.month, day.day)  # noqa: DTZ001 -- naive US/Eastern lab-clock convention (see module docstring)
        stmt = stmt.where(
            OptionSetup.ts >= start,
            OptionSetup.ts < start + timedelta(days=1),
        )
    return list(session.scalars(stmt))


def list_recent_setups(
    session: Session, *, end_day: date, days: int = 7
) -> list[OptionSetup]:
    """Setups from the ``days`` calendar days ending at ``end_day`` (inclusive),
    newest-first -- the cockpit's bounded 'recent' window, never an unpaginated
    full-table read. ``end_day`` is the caller's lab-clock today (naive-Eastern
    convention, module docstring), passed in so this module stays clock-free."""
    end = datetime(end_day.year, end_day.month, end_day.day) + timedelta(days=1)  # noqa: DTZ001 -- naive US/Eastern lab-clock convention (see module docstring)
    stmt = (
        select(OptionSetup)
        .where(OptionSetup.ts >= end - timedelta(days=days), OptionSetup.ts < end)
        .order_by(OptionSetup.ts.desc())
    )
    return list(session.scalars(stmt))

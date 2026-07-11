"""Heartbeats: Up / Late / Down from an explicit Period + Grace per scheduled job.

The cockpit's heartbeat rail (docs/plans/2026-07-05-desktop-ui-design.md): each job's
state is "time since the last observed run" measured against a stated period plus a
grace window. UNKNOWN is a first-class state -- a job with no observed run ever, or a
poller that is not configured, must never read as green. SQLite round-trips drop
tzinfo (writers stamp ``datetime.now(UTC)`` but ``EmailLog.sent_at`` comes back
naive), so a naive ``last`` is defensively read as UTC.

Queries stay local to this module: latest-run lookups for a status rail are
cockpit-view concerns, not domain CRUD, so they do NOT belong in ``db/repo.py``.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from swing_screener.db.models import EmailLog, MarketReport, Signal
from swing_screener.settings import resolve_edge_dir

# The closed four-state set (same convention as signals/actionability.py's Status):
# mypy rejects a fifth state at the source, not in some downstream renderer.
BeatState = Literal["up", "late", "down", "unknown"]


@dataclass(frozen=True, kw_only=True)
class Heartbeat:
    """One job's pulse: state + the timestamp and Period/Grace that produced it.

    ``period_s``/``grace_s`` travel with the state so the UI can show WHY a beat is
    late on hover; ``last`` is UTC-aware (or None) so the frontend formats ages
    itself. ``detail`` is a hover/debug diagnostic channel only -- the frontend
    formats ages from ``last``, so ``detail`` is not a display contract.
    Unconfigured pollers carry ``period_s == grace_s == 0`` -- an honest
    "no contract stated", never a guessed schedule.
    """

    name: str
    state: BeatState
    last: datetime | None
    period_s: int
    grace_s: int
    detail: str


def beat_state(
    last: datetime | None, now: datetime, period: timedelta, grace: timedelta
) -> BeatState:
    """The state machine: None -> unknown; within period -> up; within period+grace ->
    late; else down. A naive ``last`` is assumed UTC (SQLite drops tzinfo on round-trip).
    A ``last`` ahead of ``now`` reads "up" intentionally: EOD-anchored run dates
    legitimately sit ahead of the clock until their date is over.
    """
    if last is None:
        return "unknown"
    age = now - _as_utc(last)
    if age <= period:
        return "up"
    if age <= period + grace:
        return "late"
    return "down"


def collect_heartbeats(
    session: Session, *, now: datetime, edge_dir: Path | None = None
) -> list[Heartbeat]:
    """Assemble the Phase-1 heartbeat roster from the DB + the edge dir.

    The 72h flat period on the evening screen / daily digest is deliberate weekend
    slack: a Friday run must not read LATE on Sunday. Phase-2 TODO: a business-day
    calendar so a missed Monday run reads late Tuesday morning instead of hiding
    inside the flat 72h window. ``edge_dir`` is a test seam; ``None`` resolves via
    ``settings.resolve_edge_dir`` (the ONE shared resolution). GitHub Actions beats
    ship as explicit UNKNOWN until the Phase-2 poller exists.
    """
    resolved_edge = resolve_edge_dir(edge_dir)
    screen_last: date | None = session.scalar(select(func.max(Signal.run_date)))
    weather_last: date | None = session.scalar(select(func.max(MarketReport.run_date)))
    beats = [
        _beat("evening screen", _eod_utc(screen_last), now,
              timedelta(hours=72), timedelta(minutes=45)),
        _beat("daily digest", _latest_email(session, "daily"), now,
              timedelta(hours=72), timedelta(minutes=45)),
        _beat("weekly digest", _latest_email(session, "weekly"), now,
              timedelta(days=7), timedelta(hours=3)),
        _beat("monthly digest", _latest_email(session, "monthly"), now,
              timedelta(days=31), timedelta(days=2)),
        _beat("market weather", _eod_utc(weather_last), now,
              timedelta(days=7), timedelta(hours=3)),
        _beat("reflection verdicts", newest_verdicts_mtime(resolved_edge), now,
              timedelta(days=7), timedelta(hours=3)),
    ]
    beats += [
        Heartbeat(name=name, state="unknown", last=None, period_s=0, grace_s=0,
                  detail="poller not configured")
        for name in ("GH · optimizer", "GH · reflection", "GH · CI")
    ]
    return beats


def _beat(
    name: str, last: datetime | None, now: datetime, period: timedelta, grace: timedelta
) -> Heartbeat:
    return Heartbeat(
        name=name,
        state=beat_state(last, now, period, grace),
        last=_as_utc(last) if last is not None else None,
        period_s=int(period.total_seconds()),
        grace_s=int(grace.total_seconds()),
        detail=_age_detail(last, now),
    )


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _eod_utc(d: date | None) -> datetime | None:
    """A DB run_date has no time-of-day; anchor it at 23:59 UTC so 'ran on date D'
    can never read stale before D is even over."""
    return None if d is None else datetime.combine(d, time(23, 59), tzinfo=UTC)


def _latest_email(session: Session, kind: str) -> datetime | None:
    return session.scalar(
        select(func.max(EmailLog.sent_at)).where(EmailLog.kind == kind)
    )


def newest_verdicts_mtime(edge_dir: Path) -> datetime | None:
    """Newest ``*.verdicts.json`` mtime under ``edge_dir`` as a UTC datetime, or None
    when no verdicts file exists (a missing/empty dir is a normal setup state).
    Public: the reflection-verdicts heartbeat AND the wake channel's change token
    (cockpit/api.py) both read this -- the one shared "did reflection run?" clock."""
    mtimes = [p.stat().st_mtime for p in edge_dir.glob("*.verdicts.json")]
    return None if not mtimes else datetime.fromtimestamp(max(mtimes), tz=UTC)


def _age_detail(last: datetime | None, now: datetime) -> str:
    if last is None:
        return "no run recorded"
    # EOD-anchored dates can sit ahead of `now`; clamp so today's run reads "0h 00m".
    secs = max(0, int((now - _as_utc(last)).total_seconds()))
    return f"last {secs // 3600}h {secs % 3600 // 60:02d}m ago"

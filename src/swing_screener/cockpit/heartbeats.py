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

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from swing_screener.db.models import (
    EmailLog,
    MarketReport,
    Signal,
    SystemAudit,
    WeaknessesProfile,
)
from swing_screener.settings import resolve_edge_dir

# The closed four-state set (same convention as signals/actionability.py's Status):
# mypy rejects a fifth state at the source, not in some downstream renderer.
BeatState = Literal["up", "late", "down", "unknown"]

# NYSE full-day market holidays, 2026-2027, STATIC by design: no trading-calendar
# helper exists anywhere in this repo, and ops/eastern_gate.py already rejected the
# dependency ("an exchange-holiday calendar is not worth the dependency here") --
# the cockpit follows that precedent. Without this set every market holiday would
# light a false LATE lamp on the weekday beats, and false lights erode the only
# thing the panel sells: trust in silence.
# ANNUAL REFRESH OBLIGATION: append the next year's NYSE dates each December (the
# exchange publishes them years ahead); a stale list degrades to one false LATE
# lamp per unlisted holiday, never a crash.
_MARKET_HOLIDAYS: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed; Jul 4 is a Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving Day
    date(2026, 12, 25),  # Christmas Day
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # Martin Luther King Jr. Day
    date(2027, 2, 15),   # Washington's Birthday
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed; Jun 19 is a Saturday)
    date(2027, 7, 5),    # Independence Day (observed; Jul 4 is a Sunday)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving Day
    date(2027, 12, 24),  # Christmas Day (observed; Dec 25 is a Saturday)
})


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
    session: Session,
    *,
    now: datetime,
    edge_dir: Path | None = None,
    gh_latest: Callable[[str], tuple[datetime, str] | None] | None = None,
) -> list[Heartbeat]:
    """Assemble the heartbeat roster from the DB, the edge dir, and (optionally) GH.

    COVERAGE DOCTRINE: every scheduled job ``infra/modules/jobs.bicep`` deploys
    (all TEN) has a row here -- a deployed job missing from the rail is invisible,
    the one failure mode this module exists to prevent. Jobs whose every run
    provably writes something get a real ``max()`` watermark beat against their
    bicep cron; jobs that write only when there is something to report get the
    explicit-UNKNOWN placeholder (``_sparse_beat``) -- never a false 'down', and
    never absence.

    The two weekday jobs (evening screen, daily digest) measure against a
    BUSINESS-DAY period (``_business_period``): the next weekday after the last
    run, skipping ``_MARKET_HOLIDAYS`` -- a Friday run reads up all weekend and
    goes late only past Monday's expected run + grace, while a missed Tuesday
    reads late Tuesday night instead of hiding inside the old flat-72h weekend
    slack. ``edge_dir`` is a test seam; ``None`` resolves via
    ``settings.resolve_edge_dir`` (the ONE shared resolution). ``gh_latest`` is
    the optional GitHub Actions poller (``cockpit/gh.py``, wired by ``create_app``
    from SWING_GH_TOKEN/SWING_GH_REPO): given a workflow file it answers
    ``(completed_at, conclusion)`` or None; without it -- or when it answers None
    -- the three GH beats stay explicit UNKNOWN placeholders.
    """
    resolved_edge = resolve_edge_dir(edge_dir)
    screen_last = _eod_utc(session.scalar(select(func.max(Signal.run_date))))
    digest_last = _latest_email(session, "daily")
    weather_last: date | None = session.scalar(select(func.max(MarketReport.run_date)))
    beats = [
        _beat("evening screen", screen_last, now,
              _business_period(screen_last), timedelta(minutes=45)),
        _beat("daily digest", digest_last, now,
              _business_period(digest_last), timedelta(minutes=45)),
        _beat("weekly digest", _latest_email(session, "weekly"), now,
              timedelta(days=7), timedelta(hours=3)),
        _beat("monthly digest", _latest_email(session, "monthly"), now,
              timedelta(days=31), timedelta(days=2)),
        _beat("market weather", _eod_utc(weather_last), now,
              timedelta(days=7), timedelta(hours=3)),
        _beat("reflection verdicts", newest_verdicts_mtime(resolved_edge), now,
              timedelta(days=7), timedelta(hours=3)),
        # -- the remaining five deployed jobs (shipped without beats: each read
        # healthy by silence until here). Two have a real per-run watermark;
        # three are sparse writers and get the honest placeholder.
        # intraday-exit (hourly, ET 9-16 weekdays): EmailLog kind='exit' rows exist
        # only when an exit actually trips -- and are deduped per exit-event SET,
        # so even the write is not per-run. No watermark exists.
        _sparse_beat(
            "intraday exit",
            "alerts only when an exit trips; a quiet market and a dead job read the same"),
        # on-demand-analysis (hourly): drains the analysis_requests queue; the
        # claim/finish stamps move only when a request exists (the queue is
        # near-always empty -- 2026-07 audit), so silence proves nothing.
        _sparse_beat(
            "on-demand analysis",
            "queue-drain worker; writes only when a request is queued"),
        # journal-coach (hourly, un-gated): every run appends a WeaknessesProfile
        # row via build_profile (coach_run.main always calls it; --skip-rollup is
        # a CLI-only escape the deployed job never passes), so max(generated_at)
        # is a true per-run watermark. 15m grace covers container-start +
        # Azure-SQL-wake latency on the 1h cadence.
        _beat("journal coach",
              session.scalar(select(func.max(WeaknessesProfile.generated_at))), now,
              timedelta(hours=1), timedelta(minutes=15)),
        # journal-audit-weekly (Sat 16:00 ET): run_weekly UPSERTS the one weekly
        # SystemAudit row every run -- a clean week still writes (template
        # narrative at $0) and re-stamps generated_at -- so the kind-filtered max
        # is a true watermark. 7d/3h mirrors the other weekly beats.
        _beat("journal audit · weekly",
              session.scalar(select(func.max(SystemAudit.generated_at))
                             .where(SystemAudit.kind == "weekly")), now,
              timedelta(days=7), timedelta(hours=3)),
        # journal-audit-breach (weekdays 16:00 ET): writes a SystemAudit row ONLY
        # when it finds a NEW hard breach (cap exceeded, a disarm). A clean day
        # writes nothing, so from DB data alone a quiet stretch is
        # indistinguishable from a dead job.
        _sparse_beat(
            "journal audit · breach",
            "writes only on a hard breach; a clean day and a dead job read the same"),
    ]
    beats += [_gh_beat(name, wf, gh_latest, now) for name, wf in _GH_WORKFLOWS]
    return beats


def _sparse_beat(name: str, reason: str) -> Heartbeat:
    """The honest placeholder for a deployed job whose runs leave no per-run
    watermark (it writes only when there is something to report). Same shape as
    the GH rows' honest UNKNOWN -- state unknown, no last, 0/0 'no contract
    stated' -- with ``detail`` naming WHY the DB cannot vouch for it, so the row
    is never mistaken for a dead poller. The alternative readings are both worse:
    a time-based beat would cry false 'down' through every quiet stretch, and
    dropping the row would make a deployed job invisible."""
    return Heartbeat(name=name, state="unknown", last=None, period_s=0, grace_s=0,
                     detail=reason)


# Heartbeat name -> workflow file, the mapping the poller is asked about. The names
# are the roster contract (pinned by tests); the files are the repo's actual
# .github/workflows entries.
_GH_WORKFLOWS: tuple[tuple[str, str], ...] = (
    ("GH · optimizer", "optimize.yml"),
    ("GH · reflection", "reflect.yml"),
    ("GH · CI", "ci.yml"),
)
_GH_PERIOD = timedelta(days=7)
_GH_GRACE = timedelta(hours=3)


def _gh_beat(
    name: str,
    workflow: str,
    gh_latest: Callable[[str], tuple[datetime, str] | None] | None,
    now: datetime,
) -> Heartbeat:
    """One GH-workflow beat. No poller, or a poller answering None (unreachable
    API, no completed run): the explicit UNKNOWN placeholder -- 0/0, 'no contract
    stated' -- but ``detail`` says WHICH kind of nothing, so an operator with an
    expired token is never sent chasing a config problem that doesn't exist. CI
    is the one conclusion-driven beat: ``conclusion != "success"`` reads DOWN
    however fresh the run is -- a red CI lamp signals the conclusion, not
    staleness -- and the inequality deliberately catches timed_out /
    action_required / cancelled too, so e.g. a manually cancelled run stays red
    (with the actual conclusion in ``detail``) until the next success;
    optimizer/reflection stay purely time-based (their failure mode is 'stopped
    running', which 7d/3h already catches)."""
    if gh_latest is None:
        return Heartbeat(name=name, state="unknown", last=None, period_s=0, grace_s=0,
                         detail="poller not configured")
    run = gh_latest(workflow)
    if run is None:
        return Heartbeat(name=name, state="unknown", last=None, period_s=0, grace_s=0,
                         detail="no completed run observed or GitHub unreachable")
    completed_at, conclusion = run
    if name == "GH · CI" and conclusion != "success":
        return Heartbeat(
            name=name, state="down", last=_as_utc(completed_at),
            period_s=int(_GH_PERIOD.total_seconds()),
            grace_s=int(_GH_GRACE.total_seconds()),
            detail=f"latest run: {conclusion}",
        )
    return _beat(name, completed_at, now, _GH_PERIOD, _GH_GRACE)


def _next_expected(last: datetime) -> datetime:
    """The next business day after ``last`` -- skipping Sat/Sun and
    ``_MARKET_HOLIDAYS`` -- at the same time-of-day. Holidays are looked up on the
    UTC calendar date: the 23:59 UTC anchor is 18:59/19:59 ET of the SAME calendar
    date, so the UTC date already equals the ET trading date and a tz conversion
    would buy nothing; the same holds for the daily digest's ~16:30 ET sent_at
    anchor, which lands well before UTC midnight -- a send rescheduled past
    ~19:00 ET would cross into the next UTC date and shift weekend/holiday skips
    by a day."""
    last = _as_utc(last)
    d = last.date() + timedelta(days=1)
    while d.weekday() >= 5 or d in _MARKET_HOLIDAYS:
        d += timedelta(days=1)
    return datetime.combine(d, last.timetz())


def _business_period(last: datetime | None) -> timedelta:
    """Period for a business-day job: the gap from the last run to the next
    expected one -- 1 day midweek, 3 over a weekend, 4 over a holiday weekend.
    With no run ever there is no anchor to compute from, so fall back to the flat
    72h outer bound (the beat reads UNKNOWN then anyway; the period only states
    the contract). Known DST wrinkle: the daily digest's ET-scheduled sent_at
    shifts ~60min in UTC across a transition while grace is 45min, so the Monday
    after fall-back can read a self-healing false LATE for ~15 minutes once a
    year (the evening screen is immune -- its anchor is a fixed 23:59 UTC)."""
    if last is None:
        return timedelta(hours=72)
    return _next_expected(last) - _as_utc(last)


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
    The reflection-verdicts heartbeat's "did reflection run?" clock -- heartbeat
    only. (The wake channel's change token stats each sidecar itself via
    ``_file_watermark`` in ``routers/events.py``: per-file ns-mtimes, because this
    newest-float summary can miss an in-place rewrite within the same second.)
    Per-file stat guard (the ``_file_watermark`` posture): an in-place rewrite can
    vanish a globbed path before its stat -- skip it, never 500 the rail."""
    mtimes: list[float] = []
    for p in edge_dir.glob("*.verdicts.json"):
        try:
            mtimes.append(p.stat().st_mtime)
        except OSError:  # vanished/rewritten mid-glob: the file no longer vouches
            continue
    return None if not mtimes else datetime.fromtimestamp(max(mtimes), tz=UTC)


def _age_detail(last: datetime | None, now: datetime) -> str:
    if last is None:
        return "no run recorded"
    # EOD-anchored dates can sit ahead of `now`; clamp so today's run reads "0h 00m".
    secs = max(0, int((now - _as_utc(last)).total_seconds()))
    return f"last {secs // 3600}h {secs % 3600 // 60:02d}m ago"

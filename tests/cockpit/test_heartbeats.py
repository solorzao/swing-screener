"""Heartbeats: Up / Late / Down from explicit Period + Grace; UNKNOWN is a first-class
state (an unconfigured or dead poller must never read as green)."""

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from swing_screener.cockpit.heartbeats import (
    _MARKET_HOLIDAYS,
    Heartbeat,
    beat_state,
    collect_heartbeats,
)
from swing_screener.db.models import EmailLog, MarketReport, Signal
from swing_screener.db.session import get_engine

NOW = datetime(2026, 7, 5, 16, 0, tzinfo=UTC)

ROSTER = {
    "evening screen", "daily digest", "weekly digest", "monthly digest",
    "market weather", "reflection verdicts",
    "GH · optimizer", "GH · reflection", "GH · CI",
}


def test_state_machine_up_late_down_unknown() -> None:
    period, grace = timedelta(hours=24), timedelta(hours=1)
    up = NOW - timedelta(hours=20)
    late = NOW - timedelta(hours=24, minutes=30)
    down = NOW - timedelta(hours=26)
    assert beat_state(up, NOW, period, grace) == "up"
    assert beat_state(late, NOW, period, grace) == "late"
    assert beat_state(down, NOW, period, grace) == "down"
    assert beat_state(None, NOW, period, grace) == "unknown"
    # Boundary pins: the comparisons are inclusive -- exactly `period` old is still
    # up, exactly `period + grace` old is still late.
    assert beat_state(NOW - period, NOW, period, grace) == "up"
    assert beat_state(NOW - period - grace, NOW, period, grace) == "late"


def test_state_machine_assumes_utc_for_naive_last() -> None:
    # SQLite round-trips drop tzinfo (EmailLog.sent_at comes back naive even though
    # notify/run.py stamps datetime.now(UTC)) -- a naive `last` must be read as UTC,
    # never crash on aware-vs-naive subtraction.
    period, grace = timedelta(hours=24), timedelta(hours=1)
    naive_up = datetime(2026, 7, 5, 12, 0)     # 4h before NOW, tzinfo dropped
    naive_down = datetime(2026, 7, 3, 12, 0)   # 52h before NOW
    assert beat_state(naive_up, NOW, period, grace) == "up"
    assert beat_state(naive_down, NOW, period, grace) == "down"


def test_collect_reads_email_log_for_digests(tmp_path: Path) -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(EmailLog(sent_at=NOW - timedelta(hours=8), kind="daily",
                       subject="x", run_date=NOW.date()))
        s.commit()
        beats = {b.name: b for b in collect_heartbeats(s, now=NOW, edge_dir=tmp_path)}
    assert set(beats) == ROSTER
    assert beats["evening screen"].period_s == 72 * 3600   # weekend-slack contract
    assert beats["daily digest"].state == "up"
    assert beats["weekly digest"].state == "unknown"     # no row ever -> unknown
    assert beats["GH · optimizer"].state == "unknown"     # poller not configured
    assert beats["GH · optimizer"].detail == "poller not configured"
    assert all(isinstance(b, Heartbeat) for b in beats.values())


def test_collect_reads_signal_and_market_report_run_dates(tmp_path: Path) -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(Signal(run_date=(NOW - timedelta(days=1)).date(), ticker="AMD",
                     timeframe="1d", horizon="medium", score=0.9, rank=1,
                     trigger_close=100.0, atr=4.0, rsi=55.0, entry_floor=96.0,
                     entry_ceiling=101.0, stop=95.0, target=110.0))
        s.add(MarketReport(run_date=(NOW - timedelta(days=10)).date(),
                           ha_alignment="mixed"))
        s.commit()
        beats = {b.name: b for b in collect_heartbeats(s, now=NOW, edge_dir=tmp_path)}
    # run_date anchors at 23:59 UTC of that date: yesterday's screen is well inside 72h.
    assert beats["evening screen"].state == "up"
    # 10 days ago blows past 7d period + 3h grace.
    assert beats["market weather"].state == "down"


def test_evening_screen_anchors_run_date_at_eod_not_midnight(tmp_path: Path) -> None:
    # Divergence probe: a run_date 3 days back is 64h old anchored at 23:59 UTC
    # ("up" under 72h + 45m) but 88h old anchored at midnight ("down") -- this pins
    # the EOD anchor, which the happy-path cases above cannot distinguish.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(Signal(run_date=(NOW - timedelta(days=3)).date(), ticker="AMD",
                     timeframe="1d", horizon="medium", score=0.9, rank=1,
                     trigger_close=100.0, atr=4.0, rsi=55.0, entry_floor=96.0,
                     entry_ceiling=101.0, stop=95.0, target=110.0))
        s.commit()
        beats = {b.name: b for b in collect_heartbeats(s, now=NOW, edge_dir=tmp_path)}
    assert beats["evening screen"].state == "up"


def test_collect_reads_verdicts_file_mtime(tmp_path: Path) -> None:
    engine = get_engine("sqlite:///:memory:")
    f = tmp_path / "continuation.verdicts.json"
    f.write_text("[]", encoding="utf-8")
    ts = (NOW - timedelta(days=2)).timestamp()
    os.utime(f, (ts, ts))
    with Session(engine) as s:
        beats = {b.name: b for b in collect_heartbeats(s, now=NOW, edge_dir=tmp_path)}
    assert beats["reflection verdicts"].state == "up"
    assert beats["reflection verdicts"].last == NOW - timedelta(days=2)


def _screen_beat(run_date_: date, now: datetime, edge_dir: Path) -> Heartbeat:
    """The evening-screen beat for one seeded run_date, evaluated at ``now`` --
    a fresh in-memory DB per call so scenarios can't contaminate each other."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(Signal(run_date=run_date_, ticker="AMD", timeframe="1d", horizon="medium",
                     score=0.9, rank=1, trigger_close=100.0, atr=4.0, rsi=55.0,
                     entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0))
        s.commit()
        beats = {b.name: b for b in collect_heartbeats(s, now=now, edge_dir=edge_dir)}
    return beats["evening screen"]


def test_business_period_spans_weekend(tmp_path: Path) -> None:
    """A Friday run is up all weekend -- the next expected run is MONDAY's, so the
    beat goes late only past Mon 23:59 UTC + grace. And the tightening that
    motivated the calendar: a MONDAY run expects Tuesday's run, so a missed
    Tuesday reads down Wednesday morning instead of hiding inside a flat 72h."""
    friday = date(2026, 7, 10)
    beat = _screen_beat(friday, datetime(2026, 7, 12, 16, 0, tzinfo=UTC), tmp_path)
    assert beat.state == "up"  # Sunday afternoon: nothing was ever due on the weekend
    assert beat.period_s == 3 * 86400  # Fri 23:59 -> Mon 23:59 UTC
    assert beat.grace_s == 45 * 60
    in_grace = datetime(2026, 7, 14, 0, 30, tzinfo=UTC)   # Mon 23:59 + 31m
    past_grace = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)  # Mon 23:59 + 61m
    assert _screen_beat(friday, in_grace, tmp_path).state == "late"
    assert _screen_beat(friday, past_grace, tmp_path).state == "down"
    monday = date(2026, 7, 13)
    beat = _screen_beat(monday, datetime(2026, 7, 15, 1, 0, tzinfo=UTC), tmp_path)
    assert beat.period_s == 86400  # midweek: one business day, not 72h
    assert beat.state == "down"    # Tuesday's run missed; Wednesday 01:00 says so


def test_business_period_spans_holiday(tmp_path: Path) -> None:
    """A run the day before a listed NYSE holiday isn't due again until the day
    AFTER the holiday + grace; a Friday run before a holiday Monday spans four
    days. Holiday-blind business days would read down in both scenarios."""
    wednesday = date(2026, 11, 25)  # Thanksgiving is Thu 2026-11-26
    beat = _screen_beat(wednesday, datetime(2026, 11, 27, 20, 0, tzinfo=UTC), tmp_path)
    assert beat.state == "up"  # Friday evening, before Friday's expected 23:59 run
    assert beat.period_s == 2 * 86400  # Wed 23:59 -> Fri 23:59, skipping the holiday
    in_grace = datetime(2026, 11, 28, 0, 30, tzinfo=UTC)   # Fri 23:59 + 31m
    past_grace = datetime(2026, 11, 28, 1, 0, tzinfo=UTC)  # Fri 23:59 + 61m
    assert _screen_beat(wednesday, in_grace, tmp_path).state == "late"
    assert _screen_beat(wednesday, past_grace, tmp_path).state == "down"
    labor_friday = date(2026, 9, 4)  # Labor Day is Mon 2026-09-07
    beat = _screen_beat(labor_friday, datetime(2026, 9, 7, 20, 0, tzinfo=UTC), tmp_path)
    assert beat.state == "up"
    assert beat.period_s == 4 * 86400  # Fri 23:59 -> Tue 23:59 over the holiday weekend


def test_gh_beats_read_unknown_without_config(tmp_path: Path) -> None:
    """No poller injected (no token/repo in the environment): the three GH rows are
    the explicit UNKNOWN placeholders -- period/grace 0 ('no contract stated'),
    never a guessed schedule, and the roster itself is unchanged."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        beats = {b.name: b for b in collect_heartbeats(s, now=NOW, edge_dir=tmp_path)}
    assert set(beats) == ROSTER
    for name in ("GH · optimizer", "GH · reflection", "GH · CI"):
        b = beats[name]
        assert b.state == "unknown"
        assert b.last is None
        assert b.period_s == 0 and b.grace_s == 0
        assert b.detail == "poller not configured"


def test_gh_poller_maps_runs_to_beats(tmp_path: Path) -> None:
    """An injected poller turns the placeholders into real beats: optimizer and
    reflection are time-based (7d/3h); CI's conclusion outranks recency -- a
    fresh FAILED run reads down with the conclusion in ``detail``."""
    runs: dict[str, tuple[datetime, str]] = {
        "optimize.yml": (NOW - timedelta(days=2), "success"),
        "reflect.yml": (NOW - timedelta(days=7, hours=1), "success"),
        "ci.yml": (NOW - timedelta(hours=1), "failure"),
    }
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        beats = {b.name: b for b in collect_heartbeats(
            s, now=NOW, edge_dir=tmp_path, gh_latest=lambda wf: runs[wf])}
    assert set(beats) == ROSTER
    opt = beats["GH · optimizer"]
    assert opt.state == "up"
    assert opt.last == NOW - timedelta(days=2)
    assert opt.period_s == 7 * 86400 and opt.grace_s == 3 * 3600
    assert beats["GH · reflection"].state == "late"  # 7d1h: past period, inside grace
    ci = beats["GH · CI"]
    assert ci.state == "down"  # one hour old -- recency cannot rescue a failed run
    assert ci.detail == "latest run: failure"
    assert ci.last == NOW - timedelta(hours=1)
    assert ci.period_s == 7 * 86400  # the stated contract still travels on the beat


def test_gh_poller_none_answer_reads_unknown(tmp_path: Path) -> None:
    """A CONFIGURED poller that answers None (API unreachable, expired token, no
    completed run) degrades to the same unknown STATE and 0/0 shape as no poller
    at all -- never a stale green -- but ``detail`` must name the right kind of
    nothing: an operator with an expired token must not be sent chasing a config
    problem that doesn't exist."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        beats = {b.name: b for b in collect_heartbeats(
            s, now=NOW, edge_dir=tmp_path, gh_latest=lambda _wf: None)}
    for name in ("GH · optimizer", "GH · reflection", "GH · CI"):
        assert beats[name].state == "unknown"
        assert beats[name].period_s == 0 and beats[name].grace_s == 0
        assert beats[name].detail == "no completed run observed or GitHub unreachable"


def test_holiday_list_covers_the_current_year() -> None:
    """DELIBERATELY clock-dependent dead-man switch: _MARKET_HOLIDAYS is static by
    design (2026-2027 seeded), so this test goes red in January 2028 exactly when
    the list needs its annual refresh -- and the GH · CI beat surfaces that red in
    the cockpit itself. False lights erode trust in silence; this converts ~10
    false LATE lamps a year into one failing test."""
    assert max(d.year for d in _MARKET_HOLIDAYS) >= date.today().year

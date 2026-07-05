"""Heartbeats: Up / Late / Down from explicit Period + Grace; UNKNOWN is a first-class
state (an unconfigured or dead poller must never read as green)."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from swing_screener.cockpit.heartbeats import Heartbeat, beat_state, collect_heartbeats
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

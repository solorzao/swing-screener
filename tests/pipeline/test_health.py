"""The pure run-health helpers (``pipeline.health._freshness`` / ``health_line``).

These are the load-bearing "is the cron alive" signals: ``_freshness`` maps a last-seen date
against ``today`` to a (label, state) badge, and ``health_line`` folds freshness + the money
posture + the gate verdict into the one-line digest footer. Both are PURE -- ``today`` is
passed explicitly so the tests are deterministic, no clock, no I/O.
"""

from datetime import date

from swing_screener.pipeline.health import _freshness, health_line

TODAY = date(2026, 6, 21)


def test_freshness_recent_run_is_fresh():
    # A run two days ago is within the 4-day tolerance -> fresh.
    label, state = _freshness(date(2026, 6, 19), TODAY)
    assert state == "fresh"
    assert "2026-06-19" in label


def test_freshness_boundary_at_tolerance_is_fresh():
    # Exactly stale_days old is still fresh (<=), not stale.
    _, state = _freshness(date(2026, 6, 17), TODAY)  # 4 days
    assert state == "fresh"


def test_freshness_old_run_is_stale():
    # Ten days old is well past the 4-day tolerance -> stale.
    label, state = _freshness(date(2026, 6, 11), TODAY)
    assert state == "stale"
    assert "2026-06-11" in label


def test_freshness_just_over_tolerance_is_stale():
    _, state = _freshness(date(2026, 6, 16), TODAY)  # 5 days
    assert state == "stale"


def test_freshness_none_is_no_data():
    label, state = _freshness(None, TODAY)
    assert state == "no-data"
    assert label  # a human-readable "no data" label, not empty


def test_freshness_custom_tolerance():
    # An explicit stale_days widens/narrows the window.
    _, state = _freshness(date(2026, 6, 11), TODAY, stale_days=10)
    assert state == "fresh"


def test_health_line_fresh_off_not_ready():
    line = health_line(latest_run_date=date(2026, 6, 19), today=TODAY,
                        execution_mode="off", gate_ready=False)
    assert "Health:" in line
    assert "2026-06-19" in line
    assert "fresh" in line
    assert "execution off" in line
    assert "gate NOT READY" in line


def test_health_line_stale_live_ready():
    line = health_line(latest_run_date=date(2026, 6, 1), today=TODAY,
                        execution_mode="live", gate_ready=True)
    assert "stale" in line
    assert "execution live" in line
    assert "gate READY" in line
    assert "NOT READY" not in line


def test_health_line_no_data():
    line = health_line(latest_run_date=None, today=TODAY,
                        execution_mode="off", gate_ready=False)
    assert "no data" in line.lower()  # the no-data label, not a crash on None
    assert "execution off" in line

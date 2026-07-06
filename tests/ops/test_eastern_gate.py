"""Tests for the container ENTRYPOINT gate.

The gate decides -- at the US-Eastern wall clock -- whether a UTC cron that
fires a *superset* of times should actually run the real command. The pure
logic (``should_run`` / ``is_last_business_day``) is exercised directly; the
``main`` seam is driven with an injected fixed-clock and a recording ``exec_fn``
so tests never really exec, never touch the wall clock, and never depend on the
host timezone.
"""

import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from swing_screener.ops import eastern_gate as gate

ET = ZoneInfo("America/New_York")

PIPELINE_ARGV = ["-m", "swing_screener.pipeline.run"]


def _at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A fixed Eastern-zoned ``datetime`` for the fake clock."""
    return datetime(year, month, day, hour, minute, tzinfo=ET)


class _RecordingExec:
    """Stand-in for ``os.execv``: records calls instead of replacing the process."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, path: str, args: list[str]) -> None:
        self.calls.append((path, args))


# --------------------------------------------------------------------------- #
# should_run -- pure logic
# --------------------------------------------------------------------------- #


def test_should_run_no_hours_no_bday_always_true():
    # both gates off -> the gate is a pass-through.
    assert gate.should_run(_at(2026, 6, 15, 3, 0), hours=None, require_last_bday=False)


def test_should_run_single_hour_match():
    assert gate.should_run(_at(2026, 6, 15, 16, 30), hours="16", require_last_bday=False)


def test_should_run_single_hour_no_match():
    assert not gate.should_run(_at(2026, 6, 15, 9, 30), hours="16", require_last_bday=False)


def test_should_run_comma_list_membership():
    # hour 10 is in the set, hour 12 is not.
    assert gate.should_run(_at(2026, 6, 15, 10, 5), hours="9,10,11", require_last_bday=False)
    assert not gate.should_run(_at(2026, 6, 15, 12, 5), hours="9,10,11", require_last_bday=False)


def test_should_run_empty_hours_does_not_gate_on_hour():
    # an empty string means "don't gate on hour" -- any hour passes.
    assert gate.should_run(_at(2026, 6, 15, 3, 0), hours="", require_last_bday=False)


def test_should_run_both_conditions_must_hold():
    # 2026-05-29 (Fri) is the last business day of May 2026; 16:00 ET.
    assert gate.should_run(_at(2026, 5, 29, 16, 0), hours="16", require_last_bday=True)
    # right day, wrong hour
    assert not gate.should_run(_at(2026, 5, 29, 9, 0), hours="16", require_last_bday=True)
    # right hour, wrong day (28th is not the last business day)
    assert not gate.should_run(_at(2026, 5, 28, 16, 0), hours="16", require_last_bday=True)


# --------------------------------------------------------------------------- #
# is_last_business_day
# --------------------------------------------------------------------------- #


def test_is_last_business_day_true_when_month_ends_on_weekday():
    # 2026-06-30 is a Tuesday and the last day of June -> last business day.
    assert gate.is_last_business_day(date(2026, 6, 30))


def test_is_last_business_day_false_on_non_last_date():
    assert not gate.is_last_business_day(date(2026, 6, 29))


def test_is_last_business_day_when_month_end_is_weekend():
    # May 31 2026 is a Sunday, so the last business day is Fri May 29.
    assert gate.is_last_business_day(date(2026, 5, 29))
    assert not gate.is_last_business_day(date(2026, 5, 31))  # the actual month-end (Sunday)
    assert not gate.is_last_business_day(date(2026, 5, 30))  # Saturday
    assert not gate.is_last_business_day(date(2026, 5, 28))  # the Thursday before


# --------------------------------------------------------------------------- #
# main -- exec vs. skip seam
# --------------------------------------------------------------------------- #


def test_main_execs_when_hour_matches(monkeypatch):
    monkeypatch.setenv("RUN_IF_ET_HOUR", "16")
    monkeypatch.delenv("RUN_IF_LAST_BUSINESS_DAY", raising=False)
    rec = _RecordingExec()

    # no SystemExit on the matching path
    gate.main(PIPELINE_ARGV, now_et_fn=lambda: _at(2026, 6, 15, 16, 30), exec_fn=rec)

    assert rec.calls == [(sys.executable, [sys.executable, *PIPELINE_ARGV])]


def test_main_skips_cleanly_when_hour_does_not_match(monkeypatch):
    monkeypatch.setenv("RUN_IF_ET_HOUR", "16")
    monkeypatch.delenv("RUN_IF_LAST_BUSINESS_DAY", raising=False)
    rec = _RecordingExec()

    with pytest.raises(SystemExit) as excinfo:
        gate.main(PIPELINE_ARGV, now_et_fn=lambda: _at(2026, 6, 15, 9, 30), exec_fn=rec)

    assert excinfo.value.code == 0  # clean skip, not an error
    assert rec.calls == []  # never execs the real command


def test_main_comma_list_runs_and_skips(monkeypatch):
    monkeypatch.setenv("RUN_IF_ET_HOUR", "9,10,11")
    monkeypatch.delenv("RUN_IF_LAST_BUSINESS_DAY", raising=False)

    rec_run = _RecordingExec()
    gate.main(PIPELINE_ARGV, now_et_fn=lambda: _at(2026, 6, 15, 10, 0), exec_fn=rec_run)
    assert len(rec_run.calls) == 1

    rec_skip = _RecordingExec()
    with pytest.raises(SystemExit) as excinfo:
        gate.main(PIPELINE_ARGV, now_et_fn=lambda: _at(2026, 6, 15, 12, 0), exec_fn=rec_skip)
    assert excinfo.value.code == 0
    assert rec_skip.calls == []


def test_main_last_business_day_and_hour_both_required(monkeypatch):
    monkeypatch.setenv("RUN_IF_ET_HOUR", "16")
    monkeypatch.setenv("RUN_IF_LAST_BUSINESS_DAY", "1")

    # last business day of May 2026 (Fri the 29th) at 16:00 -> runs
    rec_run = _RecordingExec()
    gate.main(PIPELINE_ARGV, now_et_fn=lambda: _at(2026, 5, 29, 16, 0), exec_fn=rec_run)
    assert len(rec_run.calls) == 1

    # right hour but NOT the last business day -> skips cleanly
    rec_skip = _RecordingExec()
    with pytest.raises(SystemExit) as excinfo:
        gate.main(PIPELINE_ARGV, now_et_fn=lambda: _at(2026, 5, 28, 16, 0), exec_fn=rec_skip)
    assert excinfo.value.code == 0
    assert rec_skip.calls == []


def test_main_no_command_after_passing_gate_is_an_error(monkeypatch):
    monkeypatch.delenv("RUN_IF_ET_HOUR", raising=False)
    monkeypatch.delenv("RUN_IF_LAST_BUSINESS_DAY", raising=False)
    rec = _RecordingExec()

    # gate passes (no conditions) but there is nothing to exec -> non-zero error exit
    with pytest.raises(SystemExit) as excinfo:
        gate.main([], now_et_fn=lambda: _at(2026, 6, 15, 3, 0), exec_fn=rec)

    assert excinfo.value.code != 0  # a string message -> truthy/non-zero exit
    assert rec.calls == []


# --------------------------------------------------------------------------- #
# RUN_GATE_FORCE -- the manual-backfill bypass (2026-07-05 incident)
# --------------------------------------------------------------------------- #


def test_main_forced_run_bypasses_both_gates_and_logs(monkeypatch, capsys):
    """RUN_GATE_FORCE=1 execs the real command at the WRONG hour on the WRONG day,
    and announces itself loudly so a forced run is always identifiable in logs."""
    monkeypatch.setenv("RUN_IF_ET_HOUR", "9")
    monkeypatch.setenv("RUN_IF_LAST_BUSINESS_DAY", "1")
    monkeypatch.setenv("RUN_GATE_FORCE", "1")
    rec = _RecordingExec()

    # 2026-05-28 (NOT the last business day) at 20:00 (NOT hour 9) -> still execs.
    gate.main(PIPELINE_ARGV, now_et_fn=lambda: _at(2026, 5, 28, 20, 0), exec_fn=rec)

    assert rec.calls == [(sys.executable, [sys.executable, *PIPELINE_ARGV])]
    assert "eastern_gate: FORCED run" in capsys.readouterr().out


def test_main_force_only_honors_the_literal_1(monkeypatch):
    """RUN_GATE_FORCE=0 (or any non-'1' value) must NOT bypass -- a stale falsy-ish
    value left on the template can't silently disable the gate."""
    monkeypatch.setenv("RUN_IF_ET_HOUR", "9")
    monkeypatch.delenv("RUN_IF_LAST_BUSINESS_DAY", raising=False)
    monkeypatch.setenv("RUN_GATE_FORCE", "0")
    rec = _RecordingExec()

    with pytest.raises(SystemExit) as excinfo:
        gate.main(PIPELINE_ARGV, now_et_fn=lambda: _at(2026, 6, 15, 20, 0), exec_fn=rec)

    assert excinfo.value.code == 0
    assert rec.calls == []


def test_main_forced_run_with_no_command_is_still_an_error(monkeypatch):
    monkeypatch.setenv("RUN_GATE_FORCE", "1")
    rec = _RecordingExec()

    with pytest.raises(SystemExit) as excinfo:
        gate.main([], now_et_fn=lambda: _at(2026, 6, 15, 20, 0), exec_fn=rec)

    assert excinfo.value.code != 0
    assert rec.calls == []


def test_main_skip_prints_a_marker_line(monkeypatch, capsys):
    """A gate skip must not be silent: 'Succeeded but did nothing' was
    indistinguishable from a real run during the 2026-07-05 backfill."""
    monkeypatch.setenv("RUN_IF_ET_HOUR", "9")
    monkeypatch.delenv("RUN_IF_LAST_BUSINESS_DAY", raising=False)
    monkeypatch.delenv("RUN_GATE_FORCE", raising=False)
    rec = _RecordingExec()

    with pytest.raises(SystemExit) as excinfo:
        gate.main(PIPELINE_ARGV, now_et_fn=lambda: _at(2026, 6, 15, 20, 30), exec_fn=rec)

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "eastern_gate: skip" in out
    assert "20" in out          # the current ET hour is named
    assert "'9'" in out or "9" in out  # the configured hours are named


def test_main_matching_run_stays_quiet_on_stdout(monkeypatch, capsys):
    """A normal matching run adds no gate chatter -- the dark-cockpit rule: silence
    means the gate passed; only skips and forces announce themselves."""
    monkeypatch.setenv("RUN_IF_ET_HOUR", "16")
    monkeypatch.delenv("RUN_IF_LAST_BUSINESS_DAY", raising=False)
    monkeypatch.delenv("RUN_GATE_FORCE", raising=False)
    rec = _RecordingExec()

    gate.main(PIPELINE_ARGV, now_et_fn=lambda: _at(2026, 6, 15, 16, 0), exec_fn=rec)

    assert len(rec.calls) == 1
    assert "eastern_gate" not in capsys.readouterr().out

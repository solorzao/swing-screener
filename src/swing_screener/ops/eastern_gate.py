"""Container ENTRYPOINT gate: decide *at US-Eastern wall time* whether to run.

Azure Container Apps Jobs cron is UTC-only, so it cannot express "the right
US-Eastern hour across DST" or "the last business day of the month". The
workaround: a UTC cron fires a *superset* of times, and this gate -- run as the
image ENTRYPOINT -- inspects the current Eastern time and either ``exec``\\ s the
real command or exits 0 (a clean skip for the non-matching cron firings).

The image runs ``python -m swing_screener.ops.eastern_gate <command...>`` where
``<command...>`` is e.g. ``-m swing_screener.pipeline.run``. Three env vars steer
the gate:

* ``RUN_IF_ET_HOUR`` -- a comma list of Eastern hours (0-23) at which to run,
  e.g. ``"16"`` or ``"9,10,11,12,13,14,15,16"``. Empty/unset means "don't gate
  on hour".
* ``RUN_IF_LAST_BUSINESS_DAY`` -- ``"1"`` to require that today (Eastern) be the
  last Mon-Fri of its month (the monthly digest cadence).
* ``RUN_GATE_FORCE`` -- the literal ``"1"`` bypasses BOTH conditions and runs the
  command now: the manual-backfill path (``az containerapp job update
  --set-env-vars RUN_GATE_FORCE=1`` -> ``job start`` -> revert). During the
  2026-07-05 market-weather backfill, a manual ``job start`` outside the gate
  hour exited 0 having done nothing -- indistinguishable from success. Only the
  literal ``"1"`` forces, so a stale leftover value can't silently disarm the gate.

A skip prints one ``eastern_gate: skip (...)`` marker line and a forced run prints
``eastern_gate: FORCED run ...`` -- both greppable in Log Analytics, so "Succeeded"
executions that did no work are diagnosable (and a future liveness poller can tell
gate-skips from real runs). A normal matching run stays silent: the gate ``exec``\\ s
and the real command owns the output.

The clock and the ``exec`` syscall are injectable seams so tests never touch the
wall clock, the host timezone, or really replace the process.
"""

import os
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _now_et() -> datetime:
    """Current time in US-Eastern (the default clock seam, injected in tests)."""
    return datetime.now(_ET)


def is_last_business_day(d: date) -> bool:
    """True iff ``d`` is the last Mon-Fri of its month.

    Walks back from the calendar last day of the month over any trailing weekend
    to find the last weekday. Holidays are ignored on purpose -- the monthly
    digest firing a day early/late around a rare month-end holiday is harmless,
    and an exchange-holiday calendar is not worth the dependency here.
    """
    # First day of the next month, minus one day -> the last day of d's month.
    if d.month == 12:
        first_of_next = date(d.year + 1, 1, 1)
    else:
        first_of_next = date(d.year, d.month + 1, 1)
    last = first_of_next - timedelta(days=1)
    # weekday(): Mon=0 .. Sun=6; back up over Sat(5)/Sun(6) to the last weekday.
    while last.weekday() >= 5:
        last -= timedelta(days=1)
    return d == last


def _parse_hours(hours: str | None) -> set[int] | None:
    """Parse a comma list of hour ints; ``None``/empty -> ``None`` (don't gate)."""
    if hours is None:
        return None
    parts = [p.strip() for p in hours.split(",") if p.strip()]
    if not parts:
        return None
    return {int(p) for p in parts}


def should_run(now_et: datetime, *, hours: str | None, require_last_bday: bool) -> bool:
    """Return whether the real command should run at ``now_et``.

    Both configured conditions must hold. ``hours`` (when present) gates on the
    Eastern hour; ``require_last_bday`` gates on the last-business-day rule. An
    empty/missing ``hours`` does not gate on the hour at all.
    """
    wanted = _parse_hours(hours)
    if wanted is not None and now_et.hour not in wanted:
        return False
    if require_last_bday and not is_last_business_day(now_et.date()):
        return False
    return True


def main(
    argv: list[str] | None = None,
    *,
    now_et_fn: Callable[[], datetime] = _now_et,
    exec_fn: Callable[[str, list[str]], None] = os.execv,
) -> None:
    """Gate, then ``exec`` the passed command (or exit 0 to skip cleanly).

    ``argv`` are the args to run *after* the gate (e.g.
    ``["-m", "swing_screener.pipeline.run"]``); when ``None`` they come from
    ``sys.argv[1:]``. On a non-matching cron firing we ``raise SystemExit(0)`` so
    the job ends successfully without doing work. On a match we ``exec`` the same
    interpreter with ``argv``, replacing this process so the real CLI takes over.
    """
    argv = sys.argv[1:] if argv is None else argv

    hours = os.environ.get("RUN_IF_ET_HOUR")
    require_last_bday = os.environ.get("RUN_IF_LAST_BUSINESS_DAY") == "1"
    forced = os.environ.get("RUN_GATE_FORCE") == "1"  # literal "1" only, never truthiness

    now = now_et_fn()
    if forced:
        print("eastern_gate: FORCED run, bypassing RUN_IF_ET_HOUR/"
              "RUN_IF_LAST_BUSINESS_DAY", flush=True)
    elif not should_run(now, hours=hours, require_last_bday=require_last_bday):
        # The marker makes a gated no-op diagnosable: a skipped execution still ends
        # "Succeeded", and a silent one is indistinguishable from a real run
        # (2026-07-05 backfill). One line, greppable, then the clean skip.
        print(f"eastern_gate: skip (ET {now:%Y-%m-%d %H:%M}, hour {now.hour} vs "
              f"RUN_IF_ET_HOUR={hours!r}, RUN_IF_LAST_BUSINESS_DAY="
              f"{'1' if require_last_bday else 'unset'})", flush=True)
        raise SystemExit(0)  # clean skip: the cron fired at a non-matching ET hour/day
    if not argv:
        raise SystemExit("eastern_gate: no command given")
    exec_fn(sys.executable, [sys.executable, *argv])


if __name__ == "__main__":
    main()

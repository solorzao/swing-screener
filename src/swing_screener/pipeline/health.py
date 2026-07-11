"""Pure run-health helpers: the "is the cron still alive?" signal.

A silently-dead cron (the screen stops running, the digest stops sending) is the failure
mode that's hardest to notice -- nothing errors, the inbox just goes quiet. These pure
helpers turn a last-seen date into an at-a-glance badge:

- :func:`_freshness` maps a last-seen ``date`` against ``today`` to a ``(label, state)``
  pair, where ``state`` is ``"fresh"`` / ``"stale"`` / ``"no-data"``. The default 4-day
  tolerance covers a normal weekend gap (Fri screen read on Tue is still fresh).
- :func:`health_line` folds the freshness + the money posture (execution_mode) + the
  autonomy-gate verdict into ONE line for the digest footer -- pushed every day, so a
  dead cron is visible without opening the dashboard.

Both are PURE: ``today`` is passed explicitly (no clock), and they touch no DB/IO. (The
Streamlit System Health page that reused ``_freshness`` for its stale badge retired to the
cockpit, whose heartbeat rail -- ``cockpit/heartbeats.py`` -- measures liveness against
each job's own period instead; the digest footer's ``health_line`` remains this module's
consumer.)
"""

from datetime import date

# 4 CALENDAR days: a Friday screen read on the following Tuesday is still "fresh" -- the
# tolerance has to span a normal weekend (+ a holiday) without crying wolf.
DEFAULT_STALE_DAYS = 4


def _freshness(
    latest: date | None, today: date, *, stale_days: int = DEFAULT_STALE_DAYS
) -> tuple[str, str]:
    """Map a last-seen date to a ``(label, state)`` badge. PURE.

    ``state`` is ``"fresh"`` when ``latest`` is within ``stale_days`` of ``today``
    (``(today - latest).days <= stale_days``), ``"stale"`` when older, and ``"no-data"``
    when ``latest`` is None (never seen). ``label`` is a short human string carrying the
    date + the state -- the dashboard colors it red/green and the digest line embeds it.
    """
    if latest is None:
        return "no data yet", "no-data"
    age = (today - latest).days
    state = "fresh" if age <= stale_days else "stale"
    return f"{latest.isoformat()} ({state})", state


def health_line(
    *,
    latest_run_date: date | None,
    today: date,
    execution_mode: str,
    gate_ready: bool,
) -> str:
    """The one-line digest health footer. PURE.

    Folds the freshness of the latest screen run + the money posture (``execution_mode``)
    + the autonomy-gate verdict into a single pushed line, e.g.
    ``"Health: last screen 2026-06-19 (fresh) · execution off · gate NOT READY"``. Pushed
    on every digest, so a silently-dead cron (a stale run date) is visible in the inbox
    without opening the dashboard.
    """
    label, _state = _freshness(latest_run_date, today)
    screen = f"last screen {label}"
    gate = "gate READY" if gate_ready else "gate NOT READY"
    return f"Health: {screen} · execution {execution_mode} · {gate}"

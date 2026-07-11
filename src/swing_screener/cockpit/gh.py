"""Optional GitHub Actions poller for the cockpit's heartbeat rail.

The invariant: this module may NEVER crash ``/api/heartbeats``. ANY failure --
network down, bad token, rate limit, malformed payload, a workflow with no
completed runs -- degrades to ``None``, which the rail renders as the explicit
UNKNOWN placeholder. No logging handlers are installed (the cockpit may run under
pythonw with no console; a handler writing to a dead stderr kills the process
silently). Stdlib-only by design: an optional poller is not worth a dependency.

A 5-minute in-process TTL cache (keyed on ``(repo, workflow)``) sits between the
frontend's 60s heartbeat poll and the GitHub API; failures are cached too, so a
down API is probed at most once per TTL, never once per poll.
"""

import json
import time
from datetime import UTC, datetime
from urllib.request import Request, urlopen

_TTL_S = 300.0
# (repo, workflow) -> (fetched_at_monotonic, result). In-process only: the cockpit
# is a single-process app, and a stale-after-restart cache costs one extra fetch.
_CACHE: dict[tuple[str, str], tuple[float, tuple[datetime, str] | None]] = {}
_monotonic = time.monotonic  # test seam: the TTL clock, injectable per test


def latest_workflow_run(repo: str, workflow: str, token: str) -> tuple[datetime, str] | None:
    """The newest COMPLETED run of ``workflow`` in ``repo``: ``(completed_at,
    conclusion)``, or None when there is none or the API is unreachable.

    ``completed_at`` is the run's ``updated_at`` (UTC-aware; py3.12's
    ``fromisoformat`` parses the Z suffix natively). ``conclusion`` is always a
    string -- GitHub sends null for rare completed states, which reads "unknown".
    """
    key = (repo, workflow)
    now = _monotonic()
    cached = _CACHE.get(key)
    if cached is not None and now - cached[0] < _TTL_S:
        return cached[1]
    result = _fetch(repo, workflow, token)
    _CACHE[key] = (now, result)
    return result


def _fetch(repo: str, workflow: str, token: str) -> tuple[datetime, str] | None:
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs"
           "?per_page=1&status=completed")
    req = Request(  # noqa: S310 -- fixed https host, path from config, never user input
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "swing-screener-cockpit",  # GitHub 403s UA-less requests
        },
    )
    try:
        with urlopen(req, timeout=10) as resp:  # noqa: S310
            payload = json.load(resp)
        run = payload["workflow_runs"][0]
        completed = datetime.fromisoformat(run["updated_at"])
        if completed.tzinfo is None:  # defensive: the API always sends Z, but still
            completed = completed.replace(tzinfo=UTC)
        return completed, str(run["conclusion"] or "unknown")
    except Exception:  # the module invariant: degrade to None, never crash the rail
        return None

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
import os
import time
from datetime import UTC, datetime
from urllib.request import Request, urlopen

_TTL_S = 300.0
# (repo, workflow) -> (fetched_at_monotonic, result). In-process only: the cockpit
# is a single-process app, and a stale-after-restart cache costs one extra fetch.
_CACHE: dict[tuple[str, str], tuple[float, tuple[datetime, str] | None]] = {}
_monotonic = time.monotonic  # test seam: the TTL clock, injectable per test

# repo -> (fetched_at_monotonic, rows). The open-research-PRs cache: same TTL
# discipline as the workflow cache above (failures cached too -- a down API is
# probed once per TTL, and a stale answer expires with the TTL, never lingers).
_PR_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}

# Head-branch prefix -> wire ``kind``. The prefixes are the two research
# workflows' create-pull-request branch conventions (.github/workflows):
# reflect.yml opens ``reflection/edge-update``; optimize.yml opens
# ``optimizer/config-proposal``. Prefix-matched so a suffix change in either
# workflow (e.g. a per-run branch) keeps surfacing.
_RESEARCH_PR_KINDS: tuple[tuple[str, str], ...] = (
    ("reflection/", "reflection"),
    ("optimizer/", "optimizer"),
)


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


def open_research_prs() -> list[dict[str, str]]:
    """OPEN pull requests from the two research automations (reflection +
    optimizer), as ``[{"title", "url", "kind"}]`` -- the ``/api/attention``
    surface for PRs that otherwise sit invisible until someone opens GitHub.

    Config rides SWING_GH_REPO/SWING_GH_TOKEN (the same pair ``create_app``
    wires the workflow poller from), read per call so this stays importable with
    zero setup. Unconfigured, or ANY polling failure, answers ``[]`` -- the
    module invariant (honest-empty, never an exception) -- and results, failures
    included, live only one TTL, so nothing is ever stale-cached-forever."""
    repo = os.environ.get("SWING_GH_REPO")
    token = os.environ.get("SWING_GH_TOKEN")
    if not repo or not token:
        return []
    now = _monotonic()
    cached = _PR_CACHE.get(repo)
    if cached is not None and now - cached[0] < _TTL_S:
        return cached[1]
    result = _fetch_open_prs(repo, token)
    _PR_CACHE[repo] = (now, result)
    return result


def _fetch_open_prs(repo: str, token: str) -> list[dict[str, str]]:
    url = f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=100"
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "swing-screener-cockpit",  # GitHub 403s UA-less requests
        },
    )
    try:
        with urlopen(req, timeout=10) as resp:
            payload = json.load(resp)
        rows: list[dict[str, str]] = []
        for pr in payload:
            ref = pr["head"]["ref"]
            for prefix, kind in _RESEARCH_PR_KINDS:
                if ref.startswith(prefix):
                    rows.append({"title": str(pr["title"]),
                                 "url": str(pr["html_url"]), "kind": kind})
                    break
        return rows
    except Exception:  # noqa: BLE001 -- best-effort: degrade to [], never crash the strip
        return []


def _fetch(repo: str, workflow: str, token: str) -> tuple[datetime, str] | None:
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs"
           "?per_page=1&status=completed")
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "swing-screener-cockpit",  # GitHub 403s UA-less requests
        },
    )
    try:
        with urlopen(req, timeout=10) as resp:
            payload = json.load(resp)
        run = payload["workflow_runs"][0]
        completed = datetime.fromisoformat(run["updated_at"])
        if completed.tzinfo is None:  # defensive: the API always sends Z, but still
            completed = completed.replace(tzinfo=UTC)
        return completed, str(run["conclusion"] or "unknown")
    except Exception:  # noqa: BLE001 -- best-effort: degrade to None, never crash the rail
        return None

"""cockpit.gh: the optional GitHub Actions poller. The invariant under test: ANY
failure -- HTTP error, malformed payload, empty run list -- degrades to None (the
rail renders UNKNOWN), never an exception; and a 5-minute in-process TTL cache
keeps the 60s heartbeat poll from hammering the API. No test touches the network:
``urlopen`` is monkeypatched at the module seam."""

import email.message
import json
import urllib.error
from datetime import UTC, datetime
from typing import Any, Self

import pytest

from swing_screener.cockpit import gh

_RUNS_URL = ("https://api.github.com/repos/owner/repo/actions/workflows/ci.yml/runs"
             "?per_page=1&status=completed")


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The TTL caches are module-level state; isolate every test from the last --
    and scrub the poller env so a developer's real SWING_GH_* config can never
    turn a unit test into a network call."""
    monkeypatch.setattr(gh, "_CACHE", {})
    monkeypatch.setattr(gh, "_PR_CACHE", {})
    monkeypatch.delenv("SWING_GH_REPO", raising=False)
    monkeypatch.delenv("SWING_GH_TOKEN", raising=False)


def _payload(updated_at: str, conclusion: str | None) -> bytes:
    return json.dumps(
        {"workflow_runs": [{"updated_at": updated_at, "conclusion": conclusion}]}
    ).encode()


class _FakeResponse:
    """Just enough of an HTTPResponse for ``json.load`` inside a ``with`` block."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_latest_workflow_run_parses_run_and_sends_required_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success path: the ISO-Z ``updated_at`` becomes a UTC-aware datetime (py3.12's
    fromisoformat handles the Z suffix natively) and the request carries the three
    headers GitHub requires -- notably a User-Agent, without which GitHub 403s."""
    seen: list[Any] = []

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        seen.append(req)
        return _FakeResponse(_payload("2026-07-10T14:30:00Z", "success"))

    monkeypatch.setattr(gh, "urlopen", fake_urlopen)
    got = gh.latest_workflow_run("owner/repo", "ci.yml", "tok123")
    assert got == (datetime(2026, 7, 10, 14, 30, tzinfo=UTC), "success")
    req = seen[0]
    assert req.full_url == _RUNS_URL
    # urllib capitalize()s header keys on add: User-Agent is stored as User-agent.
    assert req.get_header("Authorization") == "Bearer tok123"
    assert req.get_header("Accept") == "application/vnd.github+json"
    assert req.get_header("User-agent") == "swing-screener-cockpit"


def test_null_conclusion_reads_as_unknown_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub sends ``conclusion: null`` for rare completed states -- the tuple must
    still carry a string, never None (the beat mapper compares against 'success')."""
    monkeypatch.setattr(
        gh, "urlopen",
        lambda req, timeout=0: _FakeResponse(_payload("2026-07-10T14:30:00Z", None)),
    )
    got = gh.latest_workflow_run("owner/repo", "ci.yml", "tok")
    assert got == (datetime(2026, 7, 10, 14, 30, tzinfo=UTC), "unknown")


def test_http_error_degrades_to_none_and_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 (rate limit, bad token) answers None -- and the None is CACHED, so a
    down API is probed at most once per TTL, not once per heartbeat poll."""
    calls: list[int] = []

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        calls.append(1)
        raise urllib.error.HTTPError(
            _RUNS_URL, 403, "rate limited", email.message.Message(), None)

    monkeypatch.setattr(gh, "urlopen", fake_urlopen)
    assert gh.latest_workflow_run("owner/repo", "ci.yml", "tok") is None
    assert gh.latest_workflow_run("owner/repo", "ci.yml", "tok") is None
    assert len(calls) == 1


def test_malformed_json_degrades_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gh, "urlopen", lambda req, timeout=0: _FakeResponse(b"<html>not json</html>"))
    assert gh.latest_workflow_run("owner/repo", "ci.yml", "tok") is None


def test_empty_run_list_degrades_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workflow that has never completed a run answers an empty list -- that is
    'no run observed', i.e. None, not a crash on [0]."""
    monkeypatch.setattr(
        gh, "urlopen",
        lambda req, timeout=0: _FakeResponse(json.dumps({"workflow_runs": []}).encode()),
    )
    assert gh.latest_workflow_run("owner/repo", "ci.yml", "tok") is None


def test_ttl_cache_serves_within_five_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    """One fetch per (repo, workflow) per 300s: a second call inside the TTL never
    re-fetches, a distinct workflow gets its own entry, and the entry expires."""
    clock = [1000.0]
    fetches: list[str] = []

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        fetches.append(req.full_url)
        return _FakeResponse(_payload("2026-07-10T14:30:00Z", "success"))

    monkeypatch.setattr(gh, "urlopen", fake_urlopen)
    monkeypatch.setattr(gh, "_monotonic", lambda: clock[0])

    first = gh.latest_workflow_run("owner/repo", "ci.yml", "tok")
    clock[0] += 299.0
    assert gh.latest_workflow_run("owner/repo", "ci.yml", "tok") == first
    assert len(fetches) == 1  # inside the TTL: served from cache
    gh.latest_workflow_run("owner/repo", "optimize.yml", "tok")  # own cache key
    assert len(fetches) == 2
    clock[0] += 2.0  # 301s after the first ci.yml fetch: stale, re-fetch
    gh.latest_workflow_run("owner/repo", "ci.yml", "tok")
    assert len(fetches) == 3


# ---- open_research_prs: the reflection/optimizer PR surface ----

_PRS_URL = "https://api.github.com/repos/owner/repo/pulls?state=open&per_page=100"


def _pr(ref: str, title: str, number: int) -> dict[str, Any]:
    return {"title": title, "html_url": f"https://github.com/owner/repo/pull/{number}",
            "head": {"ref": ref}}


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWING_GH_REPO", "owner/repo")
    monkeypatch.setenv("SWING_GH_TOKEN", "tok123")


def test_open_research_prs_unconfigured_is_empty_and_never_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No SWING_GH_REPO/SWING_GH_TOKEN (the fixture scrubs them) -> honest [],
    and urlopen is never touched -- unconfigured is a state, not a failure."""
    def explode(req: Any, timeout: float = 0) -> None:
        raise AssertionError("unconfigured poller must not touch the network")

    monkeypatch.setattr(gh, "urlopen", explode)
    assert gh.open_research_prs() == []
    monkeypatch.setenv("SWING_GH_REPO", "owner/repo")  # token still missing
    assert gh.open_research_prs() == []


def test_open_research_prs_filters_by_branch_convention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only heads under the two research workflows' branch conventions surface --
    reflect.yml's ``reflection/`` (it opens reflection/edge-update) and
    optimize.yml's ``optimizer/`` (optimizer/config-proposal) -- each mapped to
    its ``kind``, GitHub order preserved; a human's feature branch never rides
    along. The request carries the same three headers as the workflow poller."""
    payload = json.dumps([
        _pr("reflection/edge-update",
            "reflection: update edge playbooks from the forward book", 7),
        _pr("feature/anything", "human work, not a research PR", 8),
        _pr("optimizer/config-proposal", "optimizer: raise the gate", 9),
    ]).encode()
    seen: list[Any] = []

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        seen.append(req)
        return _FakeResponse(payload)

    _set_env(monkeypatch)
    monkeypatch.setattr(gh, "urlopen", fake_urlopen)
    assert gh.open_research_prs() == [
        {"title": "reflection: update edge playbooks from the forward book",
         "url": "https://github.com/owner/repo/pull/7", "kind": "reflection"},
        {"title": "optimizer: raise the gate",
         "url": "https://github.com/owner/repo/pull/9", "kind": "optimizer"},
    ]
    req = seen[0]
    assert req.full_url == _PRS_URL
    assert req.get_header("Authorization") == "Bearer tok123"
    assert req.get_header("Accept") == "application/vnd.github+json"
    assert req.get_header("User-agent") == "swing-screener-cockpit"


def test_open_research_prs_failure_is_empty_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module invariant, PR flavor: an HTTP error (and equally a malformed
    payload) answers [] -- never an exception into /api/attention -- and the
    failure is CACHED, so a down API is probed once per TTL, not once per poll."""
    calls: list[int] = []

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        calls.append(1)
        raise urllib.error.HTTPError(
            _PRS_URL, 403, "rate limited", email.message.Message(), None)

    _set_env(monkeypatch)
    monkeypatch.setattr(gh, "urlopen", fake_urlopen)
    assert gh.open_research_prs() == []
    assert gh.open_research_prs() == []
    assert len(calls) == 1
    monkeypatch.setattr(
        gh, "urlopen", lambda req, timeout=0: _FakeResponse(b"<html>not json</html>"))
    monkeypatch.setattr(gh, "_PR_CACHE", {})  # fresh cache: exercise the parse arm
    assert gh.open_research_prs() == []


def test_open_research_prs_ttl_expires_never_stale_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PR cache lives one TTL like the workflow cache: inside 300s the answer
    is served without a fetch; past it the entry expires and a CHANGED GitHub
    answer replaces the old one -- nothing is stale-cached-forever."""
    clock = [1000.0]
    answers = [
        json.dumps([_pr("reflection/edge-update", "round one", 1)]).encode(),
        json.dumps([]).encode(),  # the PR was merged: honest empty on refetch
    ]
    fetches: list[int] = []

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        fetches.append(1)
        return _FakeResponse(answers[len(fetches) - 1])

    _set_env(monkeypatch)
    monkeypatch.setattr(gh, "urlopen", fake_urlopen)
    monkeypatch.setattr(gh, "_monotonic", lambda: clock[0])
    first = gh.open_research_prs()
    assert [p["kind"] for p in first] == ["reflection"]
    clock[0] += 299.0
    assert gh.open_research_prs() == first
    assert len(fetches) == 1  # inside the TTL: served from cache
    clock[0] += 2.0  # 301s: stale, re-fetch sees the merge
    assert gh.open_research_prs() == []
    assert len(fetches) == 2

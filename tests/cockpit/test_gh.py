"""cockpit.gh: the optional GitHub Actions poller. The invariant under test: ANY
failure -- HTTP error, malformed payload, empty run list -- degrades to None (the
rail renders UNKNOWN), never an exception; and a 5-minute in-process TTL cache
keeps the 60s heartbeat poll from hammering the API. No test touches the network:
``urlopen`` is monkeypatched at the module seam."""

import email.message
import json
import urllib.error
from datetime import UTC, datetime
from typing import Any

import pytest

from swing_screener.cockpit import gh

_RUNS_URL = ("https://api.github.com/repos/owner/repo/actions/workflows/ci.yml/runs"
             "?per_page=1&status=completed")


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The TTL cache is module-level state; isolate every test from the last."""
    monkeypatch.setattr(gh, "_CACHE", {})


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

    def __enter__(self) -> "_FakeResponse":
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

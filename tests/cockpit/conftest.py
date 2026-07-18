"""Shared cockpit test fixtures.

The cockpit's ``create_app()`` reads ``SWING_GH_TOKEN`` / ``SWING_GH_REPO`` from
the environment at construction and, when both are set, wires REAL GitHub
pollers -- so on a dev shell that exports them (any machine that runs the
optimizer tooling), every test that builds the app makes live GitHub API calls
and the "GH · ..." heartbeats read "up" instead of the asserted "unknown".
Scrub them for the whole directory so the suite is hermetic everywhere; tests
that exercise the poller explicitly re-set the vars via ``monkeypatch.setenv``,
which runs after this autouse fixture and wins.
"""

import pytest


@pytest.fixture(autouse=True)
def _scrub_gh_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SWING_GH_TOKEN", raising=False)
    monkeypatch.delenv("SWING_GH_REPO", raising=False)

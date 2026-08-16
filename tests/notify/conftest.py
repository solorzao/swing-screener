"""Shared notify-test fixtures."""

import dataclasses

import pytest

from swing_screener.notify import run


def _patch_config(monkeypatch, **overrides):
    """Patch ``notify.run``'s StrategyConfig, LAYERING onto whatever a previously applied
    fixture already patched (it reads the current ``run.StrategyConfig``, not a fresh
    default). That is what lets ``unpark_continuation`` and ``no_conviction_floor``
    compose in either order instead of the second silently reverting the first."""
    patched = dataclasses.replace(run.StrategyConfig(), **overrides)
    monkeypatch.setattr(run, "StrategyConfig", lambda: patched)
    return patched


@pytest.fixture
def unpark_continuation(monkeypatch):
    """Flip ``surface_continuation`` back ON inside ``notify.run`` for tests that
    exercise machinery THROUGH continuation picks (deep analysis, intents/execution,
    cooldown logging, chartless warnings, PDF assembly).

    Continuation is PARKED by default -- the Q6 ceiling_atr_mult sweep completed the
    entry-economics falsification with a NULL (2026-07-25; docs/plans/
    2026-07-25-q6-q7-sweep-results.md) -- so under the default config the continuation
    pickers return [] and none of that machinery would run. Un-parking here keeps each
    test's ORIGINAL intent; the parked posture itself is pinned by
    tests/test_config.py and the parking tests in tests/notify/test_run.py.
    """
    _patch_config(monkeypatch, surface_continuation=True)


@pytest.fixture
def no_conviction_floor(monkeypatch):
    """Disarm the conviction FLOOR (``min_conviction=""``) for tests whose intent
    predates it and is orthogonal to it -- selection, cooldown, sector caps, the
    already-ran drop, PDF assembly, funnel persistence, top-N deep gating.

    Since 2026-08-16 the digest surfaces only medium/high GRADED picks, so a test that
    never arms the insight engine would otherwise surface nothing at all and stop
    testing what it was written to test (exactly the situation ``unpark_continuation``
    handles for parking). The floor's own behavior is pinned by
    tests/notify/test_run_conviction_floor.py and tests/pipeline/test_conviction_floor.py,
    and the shipped default by tests/test_config.py -- so disarming it here cannot hide
    a regression in the floor itself.
    """
    _patch_config(monkeypatch, min_conviction="")

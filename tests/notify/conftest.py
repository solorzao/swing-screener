"""Shared notify-test fixtures."""

import dataclasses

import pytest

from swing_screener.config import StrategyConfig
from swing_screener.notify import run


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
    unparked = dataclasses.replace(StrategyConfig(), surface_continuation=True)
    monkeypatch.setattr(run, "StrategyConfig", lambda: unparked)

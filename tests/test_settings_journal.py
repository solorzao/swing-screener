"""Journal v2 Coach/Auditor configuration: env-driven, default-OFF, capped."""

from swing_screener.settings import load_settings

_KEYS = [
    "SWING_COACH_ENABLED", "SWING_COACH_MAX_USD",
    "SWING_AUDIT_ENABLED", "SWING_AUDIT_MAX_USD",
]


def _clear(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)


def test_coach_and_audit_default_off_and_uncapped(monkeypatch):
    _clear(monkeypatch)
    s = load_settings()
    assert s.coach_enabled is False
    assert s.coach_max_usd is None
    assert s.audit_enabled is False
    assert s.audit_max_usd is None


def test_coach_and_audit_env_overrides(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_COACH_ENABLED", "1")
    monkeypatch.setenv("SWING_COACH_MAX_USD", "1.50")
    monkeypatch.setenv("SWING_AUDIT_ENABLED", "yes")
    monkeypatch.setenv("SWING_AUDIT_MAX_USD", "0.75")
    s = load_settings()
    assert s.coach_enabled is True
    assert s.coach_max_usd == 1.50
    assert s.audit_enabled is True
    assert s.audit_max_usd == 0.75


def test_garbage_cap_falls_back_to_uncapped(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_COACH_MAX_USD", "not-a-number")
    monkeypatch.setenv("SWING_AUDIT_MAX_USD", "")
    s = load_settings()
    assert s.coach_max_usd is None
    assert s.audit_max_usd is None

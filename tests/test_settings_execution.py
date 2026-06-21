"""Execution master-switch + hard-limit config + the pure ``resolve_execution`` resolver.

The execution adapter (later tasks) reads a single ``execution_mode`` switch plus the
optional hard-limit caps. The switch FAILS SAFE: it defaults to ``"off"`` and an
unknown/garbage value coerces back to ``"off"`` with a warning -- the screener can never
accidentally arm itself. The caps are tolerant: missing/garbage -> None. All pure: no env
read here beyond ``load_settings``.
"""

import logging

from swing_screener.settings import Limits, load_settings, resolve_execution

_KEYS = [
    "SWING_EXECUTION_MODE", "SWING_MAX_DAILY_NOTIONAL", "SWING_MAX_DAILY_LOSS",
    "SWING_MAX_CONCURRENT",
]


def _clear(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)


# --- settings parsing ---------------------------------------------------------
def test_execution_settings_default_to_off_and_no_caps(monkeypatch):
    _clear(monkeypatch)
    s = load_settings()
    assert s.execution_mode == "off"          # fail-safe: never armed by default
    assert s.max_daily_notional is None
    assert s.max_daily_loss is None
    assert s.max_concurrent is None


def test_execution_mode_paper(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_EXECUTION_MODE", "paper")
    assert load_settings().execution_mode == "paper"


def test_execution_mode_live(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_EXECUTION_MODE", "live")
    assert load_settings().execution_mode == "live"


def test_execution_mode_is_lowercased(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_EXECUTION_MODE", "PAPER")
    assert load_settings().execution_mode == "paper"


def test_execution_mode_unknown_falls_back_to_off_and_warns(monkeypatch, caplog):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_EXECUTION_MODE", "yolo")
    with caplog.at_level(logging.WARNING):
        s = load_settings()
    assert s.execution_mode == "off"          # unknown -> off, never armed
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "yolo" in caplog.text


def test_execution_caps_parsed_from_env(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_MAX_DAILY_NOTIONAL", "25000")
    monkeypatch.setenv("SWING_MAX_DAILY_LOSS", "1500")
    monkeypatch.setenv("SWING_MAX_CONCURRENT", "5")
    s = load_settings()
    assert s.max_daily_notional == 25000.0
    assert s.max_daily_loss == 1500.0
    assert s.max_concurrent == 5


def test_execution_caps_garbage_falls_back_to_none(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_MAX_DAILY_NOTIONAL", "lots")
    monkeypatch.setenv("SWING_MAX_DAILY_LOSS", "")
    monkeypatch.setenv("SWING_MAX_CONCURRENT", "x")
    s = load_settings()
    assert s.max_daily_notional is None       # unparseable float -> None
    assert s.max_daily_loss is None
    assert s.max_concurrent is None           # unparseable int -> None


# --- resolve_execution (pure) -------------------------------------------------
def test_resolve_execution_default_is_off_and_empty_limits(monkeypatch):
    _clear(monkeypatch)
    mode, limits = resolve_execution(load_settings())
    assert mode == "off"
    assert limits == Limits(None, None, None)


def test_resolve_execution_passes_mode_and_caps_through(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_EXECUTION_MODE", "paper")
    monkeypatch.setenv("SWING_MAX_DAILY_NOTIONAL", "25000")
    monkeypatch.setenv("SWING_MAX_DAILY_LOSS", "1500")
    monkeypatch.setenv("SWING_MAX_CONCURRENT", "5")
    mode, limits = resolve_execution(load_settings())
    assert mode == "paper"
    assert limits == Limits(
        max_daily_notional=25000.0, max_daily_loss=1500.0, max_concurrent=5)


def test_resolve_execution_unknown_mode_resolves_off(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_EXECUTION_MODE", "yolo")
    mode, _limits = resolve_execution(load_settings())
    assert mode == "off"                      # the fail-safe carries through the resolver

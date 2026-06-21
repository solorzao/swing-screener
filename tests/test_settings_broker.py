"""Broker config + the pure real-money arming locks (Phase 4, Task 3).

Real money is gated behind THREE independent locks, any one of which can refuse:
``execution_mode == "live"`` AND the explicit ``allow_real_money`` flag AND a ready
autonomy gate. ``can_arm_real_money`` arms only when all three hold and names the FIRST
failing lock otherwise. ``real_money_limits_ok`` is the separate mandate that a real-money
endpoint may not run with an unbounded cap -- every limit must be set. Both pure: no env
read beyond ``load_settings``, no DB, no broker IO.
"""

from swing_screener.settings import (
    Limits,
    can_arm_real_money,
    load_settings,
    real_money_limits_ok,
)

_KEYS = ["SWING_BROKER", "SWING_BROKER_ALLOW_REAL_MONEY"]


def _clear(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)


# --- settings parsing ---------------------------------------------------------
def test_broker_settings_default_to_empty_and_disallowed(monkeypatch):
    _clear(monkeypatch)
    s = load_settings()
    assert s.broker == ""               # no broker by default
    assert s.allow_real_money is False  # the LOUD real-money flag is off by default


def test_broker_is_stripped_and_lowercased(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_BROKER", "  Alpaca  ")
    assert load_settings().broker == "alpaca"


def test_allow_real_money_parses_via_true_set(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_BROKER_ALLOW_REAL_MONEY", "yes")
    assert load_settings().allow_real_money is True


def test_allow_real_money_unknown_value_is_false(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_BROKER_ALLOW_REAL_MONEY", "maybe")
    assert load_settings().allow_real_money is False


# --- can_arm_real_money (pure, three independent locks) ------------------------
def _live_allow(monkeypatch):
    """Settings with two of three locks set (mode=live, allow=True). Gate is the arg."""
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_EXECUTION_MODE", "live")
    monkeypatch.setenv("SWING_BROKER_ALLOW_REAL_MONEY", "true")
    return load_settings()


def test_arm_ok_when_all_three_locks_hold(monkeypatch):
    s = _live_allow(monkeypatch)
    ok, reason = can_arm_real_money(s, gate_ready=True)
    assert ok is True
    assert reason == ""


def test_arm_refused_when_mode_not_live(monkeypatch):
    # only this lock missing: mode is paper, allow=True, gate ready
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_EXECUTION_MODE", "paper")
    monkeypatch.setenv("SWING_BROKER_ALLOW_REAL_MONEY", "true")
    ok, reason = can_arm_real_money(load_settings(), gate_ready=True)
    assert ok is False
    assert "execution_mode" in reason


def test_arm_refused_when_allow_flag_unset(monkeypatch):
    # only this lock missing: mode=live, allow flag off, gate ready
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_EXECUTION_MODE", "live")
    ok, reason = can_arm_real_money(load_settings(), gate_ready=True)
    assert ok is False
    assert "SWING_BROKER_ALLOW_REAL_MONEY" in reason


def test_arm_refused_when_gate_not_ready(monkeypatch):
    # only this lock missing: mode=live, allow=True, gate NOT ready
    s = _live_allow(monkeypatch)
    ok, reason = can_arm_real_money(s, gate_ready=False)
    assert ok is False
    assert "autonomy gate" in reason


def test_arm_refused_when_nothing_set(monkeypatch):
    # all three missing -> still refuses (names the first lock)
    _clear(monkeypatch)
    ok, reason = can_arm_real_money(load_settings(), gate_ready=False)
    assert ok is False
    assert reason != ""


# --- real_money_limits_ok (pure mandate: every cap must be set) ---------------
def test_limits_ok_when_all_caps_set():
    limits = Limits(max_daily_notional=25000.0, max_daily_loss=1500.0, max_concurrent=5)
    ok, reason = real_money_limits_ok(limits)
    assert ok is True
    assert reason == ""


def test_limits_not_ok_when_notional_unset():
    limits = Limits(max_daily_notional=None, max_daily_loss=1500.0, max_concurrent=5)
    ok, reason = real_money_limits_ok(limits)
    assert ok is False
    assert "max_daily_notional" in reason


def test_limits_not_ok_when_daily_loss_unset():
    limits = Limits(max_daily_notional=25000.0, max_daily_loss=None, max_concurrent=5)
    ok, reason = real_money_limits_ok(limits)
    assert ok is False
    assert "max_daily_loss" in reason


def test_limits_not_ok_when_concurrent_unset():
    limits = Limits(max_daily_notional=25000.0, max_daily_loss=1500.0, max_concurrent=None)
    ok, reason = real_money_limits_ok(limits)
    assert ok is False
    assert "max_concurrent" in reason

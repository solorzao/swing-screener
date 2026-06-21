"""Per-trade risk-unit config + the pure ``resolve_risk_unit`` resolver (Task 6, Part A).

The resolver turns the (env-driven) account/risk settings into the single
``(risk_unit_dollars, max_shares)`` pair the insight engine sizes from. Precedence
is deliberate: an explicit fixed dollar risk wins; else a percent of equity; else
0.0 -- which the sizer reads as "unconfigured" and renders R-multiples rather than
a guessed dollar. All pure: no env read here beyond ``load_settings``.
"""

from swing_screener.settings import load_settings, resolve_risk_unit

_KEYS = [
    "SWING_ACCOUNT_EQUITY", "SWING_RISK_PER_TRADE_DOLLARS", "SWING_RISK_PCT",
    "SWING_MAX_SHARES",
]


def _clear(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)


# --- settings parsing ---------------------------------------------------------
def test_risk_settings_default_to_none_and_one_pct(monkeypatch):
    _clear(monkeypatch)
    s = load_settings()
    assert s.account_equity is None
    assert s.risk_per_trade_dollars is None
    assert s.risk_pct == 0.01
    assert s.max_shares is None


def test_risk_settings_env_overrides(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_ACCOUNT_EQUITY", "50000")
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "750")
    monkeypatch.setenv("SWING_RISK_PCT", "0.02")
    monkeypatch.setenv("SWING_MAX_SHARES", "300")
    s = load_settings()
    assert s.account_equity == 50000.0
    assert s.risk_per_trade_dollars == 750.0
    assert s.risk_pct == 0.02
    assert s.max_shares == 300


def test_risk_settings_garbage_falls_back(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_ACCOUNT_EQUITY", "notanumber")
    monkeypatch.setenv("SWING_RISK_PCT", "")
    monkeypatch.setenv("SWING_MAX_SHARES", "x")
    s = load_settings()
    assert s.account_equity is None       # unparseable float -> None
    assert s.risk_pct == 0.01             # unparseable pct -> default
    assert s.max_shares is None           # unparseable int -> None


# --- resolve_risk_unit (pure) -------------------------------------------------
def test_resolve_prefers_fixed_dollars(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "750")
    monkeypatch.setenv("SWING_ACCOUNT_EQUITY", "50000")   # ignored when fixed $ set
    monkeypatch.setenv("SWING_RISK_PCT", "0.02")
    risk_unit, max_sh = resolve_risk_unit(load_settings())
    assert risk_unit == 750.0
    assert max_sh is None


def test_resolve_equity_times_pct_when_no_fixed_dollars(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_ACCOUNT_EQUITY", "50000")
    monkeypatch.setenv("SWING_RISK_PCT", "0.02")
    risk_unit, max_sh = resolve_risk_unit(load_settings())
    assert risk_unit == 1000.0            # 50000 * 0.02
    assert max_sh is None


def test_resolve_neither_set_is_zero(monkeypatch):
    _clear(monkeypatch)
    risk_unit, max_sh = resolve_risk_unit(load_settings())
    assert risk_unit == 0.0               # -> R-multiples, never a guessed dollar
    assert max_sh is None


def test_resolve_passes_max_shares_through(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "500")
    monkeypatch.setenv("SWING_MAX_SHARES", "200")
    risk_unit, max_sh = resolve_risk_unit(load_settings())
    assert risk_unit == 500.0
    assert max_sh == 200

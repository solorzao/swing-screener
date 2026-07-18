"""Market Weather env off-switch (SWING_MARKET_REPORT): default-ON to preserve today's
behavior (StrategyConfig.market_report_enabled is the code-level switch it ANDs with),
with an honest fail-safe parse -- only an EXPLICIT off value disables a scheduled report."""

from swing_screener.settings import load_settings


def test_market_report_defaults_on(monkeypatch):
    monkeypatch.delenv("SWING_MARKET_REPORT", raising=False)
    assert load_settings().market_report_enabled is True   # absent = today's behavior


def test_market_report_explicit_off_values(monkeypatch):
    for v in ("0", "false", "off", "no", " FALSE "):
        monkeypatch.setenv("SWING_MARKET_REPORT", v)
        assert load_settings().market_report_enabled is False, v


def test_market_report_garbage_stays_on(monkeypatch):
    # A typo'd value must not silently kill the Sunday report -- mirror of the
    # bracket_orders parse: anything that is not an explicit off keeps the default.
    for v in ("1", "true", "on", "garbage"):
        monkeypatch.setenv("SWING_MARKET_REPORT", v)
        assert load_settings().market_report_enabled is True, v

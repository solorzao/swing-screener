"""Deep-analysis configuration: env-driven, default-OFF, with safe fallbacks."""

from swing_screener.settings import load_settings

_KEYS = [
    "SWING_DEEP_ANALYSIS", "SWING_ANALYSIS_MODEL", "SWING_ANALYSIS_REASONING",
    "SWING_DEEP_ANALYSIS_TOP_N", "SWING_DEEP_ANALYSIS_KINDS", "SWING_ANALYSIS_MAX_SEARCHES",
    "SWING_DEEP_ANALYSIS_MAX_USD",
]


def _clear(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)


def test_deep_analysis_defaults_are_off_and_opus(monkeypatch):
    _clear(monkeypatch)
    s = load_settings()
    assert s.deep_analysis_enabled is False           # master switch defaults OFF
    assert s.analysis_model == "claude-opus-4-8"
    assert s.analysis_reasoning == "high"             # max reasoning by default
    assert s.deep_analysis_top_n == 3
    assert s.deep_analysis_kinds == frozenset({"daily", "weekly", "monthly"})
    assert s.analysis_max_searches == 4               # bounds web-search cost
    assert s.deep_analysis_max_usd is None            # no spend ceiling by default


def test_deep_analysis_env_overrides(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_DEEP_ANALYSIS", "1")
    monkeypatch.setenv("SWING_ANALYSIS_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("SWING_ANALYSIS_REASONING", "low")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "4")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_KINDS", "daily, weekly")
    monkeypatch.setenv("SWING_ANALYSIS_MAX_SEARCHES", "2")
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_MAX_USD", "1.50")
    s = load_settings()
    assert s.deep_analysis_enabled is True
    assert s.analysis_model == "claude-sonnet-4-6"
    assert s.analysis_reasoning == "low"
    assert s.deep_analysis_top_n == 4
    assert s.deep_analysis_kinds == frozenset({"daily", "weekly"})
    assert s.analysis_max_searches == 2
    assert s.deep_analysis_max_usd == 1.50            # per-run spend ceiling


def test_invalid_reasoning_falls_back_to_high(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_ANALYSIS_REASONING", "bogus")
    assert load_settings().analysis_reasoning == "high"


def test_invalid_int_envs_fall_back_to_defaults(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_TOP_N", "abc")
    monkeypatch.setenv("SWING_ANALYSIS_MAX_SEARCHES", "")
    s = load_settings()
    assert s.deep_analysis_top_n == 3 and s.analysis_max_searches == 4


def test_invalid_max_usd_falls_back_to_no_ceiling(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SWING_DEEP_ANALYSIS_MAX_USD", "not-a-number")
    assert load_settings().deep_analysis_max_usd is None   # garbage -> no ceiling (fail safe)

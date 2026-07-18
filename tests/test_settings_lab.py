"""SWING_LAB_REASONING -- the TICKER LAB effort knob. Defaults to 'max' (the
lab exists for the user's own deep research); invalid values coerce back UP to
'max', never silently weaker; the widened _REASONING set accepts xhigh/max for
the digest knob too."""

from swing_screener.settings import load_settings


def test_lab_reasoning_defaults_to_max(monkeypatch) -> None:
    monkeypatch.delenv("SWING_LAB_REASONING", raising=False)
    assert load_settings().lab_reasoning == "max"


def test_lab_reasoning_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SWING_LAB_REASONING", "medium")
    assert load_settings().lab_reasoning == "medium"


def test_lab_reasoning_invalid_coerces_to_max(monkeypatch) -> None:
    monkeypatch.setenv("SWING_LAB_REASONING", "bogus")
    assert load_settings().lab_reasoning == "max"


def test_analysis_reasoning_accepts_the_extended_efforts(monkeypatch) -> None:
    monkeypatch.setenv("SWING_ANALYSIS_REASONING", "max")
    assert load_settings().analysis_reasoning == "max"
    monkeypatch.setenv("SWING_ANALYSIS_REASONING", "xhigh")
    assert load_settings().analysis_reasoning == "xhigh"

"""The conviction analyst reads the setup's VOLUME footprint.

Until now volume reached the analyst only as pixels inside the chart image, so it could
not reason about it explicitly or cite it in a nudge reason. These three facts -- thrust,
dry-up, pocket pivot -- are the volume story of the run-up into the trigger.

They INFORM the +-1 nudge and nothing else: they do not move the deterministic baseline,
gate surfacing, or enter the ranking score. "Volume dry-up" and "pocket pivot" are still
unrun edge-discovery experiments, so they earn teeth only by certifying on the record the
analyst's own nudges build.
"""

from swing_screener.notify.analysis import (
    _CONVICTION_SYSTEM,
    SignalFacts,
    _conviction_prompt,
    _facts_lines,
)


def _facts(**over) -> SignalFacts:
    base = dict(
        ticker="AMD", timeframe="1d", trade_type="medium", score=0.85, mtf_aligned=True,
        quality_tier="high", volatility_tier="med", oversold=False, trigger_close=100.0,
        atr=4.0, rsi=55.0, entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
    )
    base.update(over)
    return SignalFacts(**base)


def test_the_volume_footprint_reaches_the_analyst() -> None:
    lines = _facts_lines(_facts(rvol_trigger=1.8, rvol_pullback=0.62, pocket_pivot=True))

    assert "1.8" in lines and "0.62" in lines
    assert "volume" in lines.lower()


def test_a_dry_up_is_labelled_so_the_direction_is_unambiguous() -> None:
    """A bare "0.62x" leaves the model to infer whether lower is better. Naming the
    shape is what makes it usable evidence rather than a number to pattern-match."""
    lines = _facts_lines(_facts(rvol_trigger=1.8, rvol_pullback=0.62, pocket_pivot=True))

    assert "dry-up" in lines.lower()


def test_a_heavy_pullback_is_not_labelled_a_dry_up() -> None:
    lines = _facts_lines(_facts(rvol_trigger=1.1, rvol_pullback=1.4, pocket_pivot=False))

    assert "dry-up" not in lines.lower()


def test_an_unmeasured_volume_reading_is_omitted_not_faked() -> None:
    """Absent volume must not render as a neutral 1.0 -- the analyst would weigh a
    fabricated "average volume" as real evidence. Legacy rows and NaN-volume index
    tickers both land here."""
    lines = _facts_lines(_facts())   # all three default to None

    assert "volume" not in lines.lower()
    assert "not measured" not in lines.lower()   # silence, not a placeholder fact


def test_a_partially_measured_profile_reports_only_what_it_has() -> None:
    lines = _facts_lines(_facts(rvol_trigger=1.8))

    assert "1.8" in lines
    assert "dry-up" not in lines.lower()


def test_the_prompt_carries_the_volume_facts() -> None:
    prompt = _conviction_prompt(
        _facts(rvol_trigger=1.8, rvol_pullback=0.62, pocket_pivot=True),
        "medium", "playbook", "context")

    assert "1.8" in prompt and "0.62" in prompt


def test_the_system_prompt_asks_the_analyst_to_weigh_volume() -> None:
    """Facts alone are inert -- without an instruction the model may ignore them. The
    system prompt must name volume as something to weigh and to cite when it moves."""
    assert "volume" in _CONVICTION_SYSTEM.lower()

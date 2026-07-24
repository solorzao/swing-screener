"""Playbook drift contract: the check is STRUCTURAL (tier-section membership of
``dimension=bucket`` tokens) and NEVER numeric -- the author rounds and reformats
every figure, so a faithful file with different-looking numbers must read ok, while
a token filed under the wrong tier must flag even though it appears in the md."""

from dataclasses import replace

from swing_screener.cockpit.playbooks import (
    DriftReport,
    MissingToken,
    playbook_drift,
)
from swing_screener.pipeline.reflect import Verdict, render_edge_file


def _verdict(**over: object) -> Verdict:
    base: dict = {
        "play_type": "reversal", "dimension": "market_trend", "bucket": "bear",
        "tier": "replay_screened", "n": 2387, "expectancy_r": 0.19589562, "ci_low": 0.10368658,
        "n_clusters": 100, "source": "replay",
    }
    base.update(over)
    return Verdict(**base)


def test_drift_ok_on_faithful_md() -> None:
    """The reflection's own renderer places every token under its tier's section,
    so drift over (render_edge_file output, same verdicts) is ok BY CONSTRUCTION
    -- across all three tiers at once."""
    verdicts = [
        _verdict(),
        _verdict(dimension="volatility_tier", bucket="low", tier="hunch",
                 source="none"),
        _verdict(dimension="volatility_tier", bucket="high",
                 tier="forward_confirmed", source="forward"),
    ]
    md = render_edge_file("reversal", "thesis text", verdicts, n_closed_now=42)
    assert playbook_drift(md, verdicts) == DriftReport(ok=True, missing=())


def test_drift_flags_missing_token_in_wrong_tier_section() -> None:
    """A token filed under the WRONG tier is exactly as drifted as an absent one:
    the sidecar says replay_screened but the (hand-edited / stale) md carries the
    condition under Hunches. Mutation-proof: searching the WHOLE md instead of
    the tier's section finds the token (it IS in the file) and reads a false ok."""
    sidecar = _verdict()  # replay_screened is the ground truth
    stale_md = render_edge_file(
        "reversal", "thesis", [replace(sidecar, tier="hunch")], n_closed_now=42)
    report = playbook_drift(stale_md, [sidecar])
    assert report == DriftReport(
        ok=False,
        missing=(MissingToken(token="market_trend=bear", tier="replay_screened"),),
    )


def test_drift_is_structural_never_numeric() -> None:
    """The author rounds: an md rendered from DIFFERENT numbers (same token, same
    tier) still reads ok. Mutation-proof: any numeric comparison between sidecar
    figures and md text flags this faithful-by-tier file and fails here."""
    sidecar = _verdict()
    rounded = replace(sidecar, expectancy_r=0.2, ci_low=0.1, n=2400, n_clusters=99)
    md = render_edge_file("reversal", "thesis", [rounded], n_closed_now=42)
    assert playbook_drift(md, [sidecar]).ok


def test_drift_empty_md_flags_everything_and_empty_verdicts_are_ok() -> None:
    """A blank/missing md with a non-empty sidecar reads all-amber (every token is
    missing from an empty section); a sidecar with NOTHING graded is honestly ok
    -- there is nothing the prose could contradict."""
    sidecar = _verdict()
    report = playbook_drift("", [sidecar])
    assert report == DriftReport(
        ok=False,
        missing=(MissingToken(token="market_trend=bear", tier="replay_screened"),),
    )
    assert playbook_drift("## Thesis\n\nwords\n", []) == DriftReport(
        ok=True, missing=())


def test_drift_unknown_tier_always_flags() -> None:
    """A tier outside the closed three-section map has no legitimate home in the
    md, so it flags even when the token appears in EVERY section -- an unknown
    tier must never read green (the UNKNOWN-never-green posture)."""
    everywhere = render_edge_file(
        "reversal", "thesis",
        [_verdict(tier="forward_confirmed", source="forward"),
         _verdict(tier="replay_screened"),
         _verdict(tier="hunch", source="none")],
        n_closed_now=1,
    )
    rogue = _verdict(tier="galaxy_brain")
    report = playbook_drift(everywhere, [rogue])
    assert report.missing == (
        MissingToken(token="market_trend=bear", tier="galaxy_brain"),)

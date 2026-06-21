"""Tests for the PURE deterministic core of the insight engine (``pipeline.insight``).

No LLM, no I/O: the baseline conviction maps a pick's buckets onto the playbook
verdicts, and the R-based sizing scales 1R by conviction. The score band MUST key
off the real published labels (``_score_labels(_SCORE_EDGES)``) -- never a duplicated
banding -- so a calibration-table rename can't silently desync the grader.
"""

from swing_screener.pipeline.insight import (
    _score_band,
    conviction_baseline,
    size_order,
)
from swing_screener.pipeline.reflect import Verdict


def _verdict(*, dimension: str, bucket: str, tier: str, ci_low: float,
            expectancy_r: float = 0.5, n: int = 30) -> Verdict:
    """A verdict with the grading-relevant fields set; the display-only fields get
    inert defaults so the baseline can key on dimension/bucket/tier/ci_low."""
    return Verdict(
        play_type="continuation", dimension=dimension, bucket=bucket, tier=tier,
        n=n, expectancy_r=expectancy_r, ci_low=ci_low, n_clusters=10,
        source="forward" if tier == "forward_confirmed" else "replay",
    )


# ---------------------------------------------------------------------------
# _score_band: keyed to the real published lower-inclusive labels.
# ---------------------------------------------------------------------------
def test_score_band_mid() -> None:
    assert _score_band(0.55) == "0.50-0.60"


def test_score_band_exact_edge_goes_up() -> None:
    # lower-inclusive: a score exactly on an edge falls into the higher band.
    assert _score_band(0.80) == "0.80-1.00"


def test_score_band_low() -> None:
    assert _score_band(0.40) == "0.00-0.50"


# ---------------------------------------------------------------------------
# conviction_baseline: pick buckets vs playbook verdicts.
# ---------------------------------------------------------------------------
def test_baseline_forward_confirmed_is_high_and_names_the_edge() -> None:
    # score 0.85 -> "0.80-1.00" band, which matches a forward_confirmed verdict (ci_low>0).
    verdicts = [_verdict(dimension="score", bucket="0.80-1.00",
                         tier="forward_confirmed", ci_low=0.42, expectancy_r=0.61, n=44)]
    conviction, edge = conviction_baseline(
        score=0.85, volatility_tier="med", market_trend="bull", verdicts=verdicts)
    assert conviction == "high"
    # the edge label must name the matched edge (dimension=bucket, tier, R, n).
    assert "score=0.80-1.00" in edge
    assert "forward_confirmed" in edge
    assert "+0.61R" in edge
    assert "n=44" in edge


def test_baseline_replay_screened_is_medium() -> None:
    verdicts = [_verdict(dimension="volatility_tier", bucket="med",
                         tier="replay_screened", ci_low=0.30)]
    conviction, edge = conviction_baseline(
        score=0.55, volatility_tier="med", market_trend=None, verdicts=verdicts)
    assert conviction == "medium"
    assert "volatility_tier=med" in edge
    assert "replay_screened" in edge


def test_baseline_forward_confirmed_negative_ci_is_avoid() -> None:
    verdicts = [_verdict(dimension="score", bucket="0.50-0.60",
                         tier="forward_confirmed", ci_low=-0.15, expectancy_r=-0.05)]
    conviction, edge = conviction_baseline(
        score=0.55, volatility_tier="low", market_trend="bear", verdicts=verdicts)
    assert conviction == "avoid"
    assert "score=0.50-0.60" in edge


def test_baseline_no_match_is_medium_neutral() -> None:
    # the only verdict is for a band the pick is NOT in.
    verdicts = [_verdict(dimension="score", bucket="0.80-1.00",
                         tier="forward_confirmed", ci_low=0.5)]
    conviction, edge = conviction_baseline(
        score=0.55, volatility_tier="med", market_trend="bull", verdicts=verdicts)
    assert conviction == "medium"
    assert edge == "no matching playbook edge"


def test_baseline_market_trend_none_does_not_match_a_trend_verdict() -> None:
    # regime unknown: a bull/bear verdict must NOT match (None != "bull").
    verdicts = [_verdict(dimension="market_trend", bucket="bull",
                         tier="forward_confirmed", ci_low=0.5)]
    conviction, edge = conviction_baseline(
        score=0.55, volatility_tier="med", market_trend=None, verdicts=verdicts)
    assert conviction == "medium"
    assert edge == "no matching playbook edge"


def test_baseline_multiple_confirmed_names_highest_ci_low() -> None:
    # both the score band AND the volatility tier confirm; the higher-ci_low edge wins.
    verdicts = [
        _verdict(dimension="score", bucket="0.80-1.00",
                 tier="forward_confirmed", ci_low=0.20, expectancy_r=0.40, n=30),
        _verdict(dimension="volatility_tier", bucket="low",
                 tier="forward_confirmed", ci_low=0.55, expectancy_r=0.70, n=50),
    ]
    conviction, edge = conviction_baseline(
        score=0.90, volatility_tier="low", market_trend="bull", verdicts=verdicts)
    assert conviction == "high"
    assert "volatility_tier=low" in edge  # the 0.55-ci_low edge, not the 0.20 one
    assert "+0.70R" in edge


def test_baseline_negative_confirmed_beats_a_positive_confirmed() -> None:
    # a matching forward_confirmed with NEGATIVE ci_low is an avoid even if another
    # matching forward_confirmed is positive -- the warning wins.
    verdicts = [
        _verdict(dimension="score", bucket="0.80-1.00",
                 tier="forward_confirmed", ci_low=0.50),
        _verdict(dimension="volatility_tier", bucket="high",
                 tier="forward_confirmed", ci_low=-0.10),
    ]
    conviction, edge = conviction_baseline(
        score=0.90, volatility_tier="high", market_trend="bull", verdicts=verdicts)
    assert conviction == "avoid"
    assert "volatility_tier=high" in edge


def test_baseline_confirmed_outranks_screened() -> None:
    # a confirmed match and a screened match both exist -> confirmed (high) wins.
    verdicts = [
        _verdict(dimension="score", bucket="0.80-1.00",
                 tier="forward_confirmed", ci_low=0.30),
        _verdict(dimension="volatility_tier", bucket="low",
                 tier="replay_screened", ci_low=0.90),
    ]
    conviction, _edge = conviction_baseline(
        score=0.90, volatility_tier="low", market_trend="bull", verdicts=verdicts)
    assert conviction == "high"


def test_baseline_ignores_hunch_tier() -> None:
    # a matching hunch is neither confirmed nor screened -> neutral medium.
    verdicts = [_verdict(dimension="score", bucket="0.80-1.00",
                         tier="hunch", ci_low=0.30)]
    conviction, edge = conviction_baseline(
        score=0.90, volatility_tier="med", market_trend="bull", verdicts=verdicts)
    assert conviction == "medium"
    assert edge == "no matching playbook edge"


# ---------------------------------------------------------------------------
# size_order: conviction-scaled R-based size with guards.
# ---------------------------------------------------------------------------
def test_size_high_full_risk_unit() -> None:
    # risk_unit $250, per-share $2 -> high uses the full unit: floor(250/2) = 125, $250 risk.
    shares, risk = size_order(
        conviction="high", entry_ceiling=12.0, stop=10.0, risk_unit_dollars=250.0)
    assert shares == 125
    assert risk == 250.0


def test_size_medium_half_risk_unit() -> None:
    # medium scales the unit by 0.5: budget $125 -> floor(125/2) = 62, 62*$2 = $124.
    shares, risk = size_order(
        conviction="medium", entry_ceiling=12.0, stop=10.0, risk_unit_dollars=250.0)
    assert shares == 62
    assert risk == 124.0


def test_size_low_quarter_risk_unit() -> None:
    # low scales by 0.25: budget $62.5 -> floor(62.5/2) = 31, 31*$2 = $62.
    shares, risk = size_order(
        conviction="low", entry_ceiling=12.0, stop=10.0, risk_unit_dollars=250.0)
    assert shares == 31
    assert risk == 62.0


def test_size_avoid_is_zero() -> None:
    shares, risk = size_order(
        conviction="avoid", entry_ceiling=12.0, stop=10.0, risk_unit_dollars=250.0)
    assert shares == 0
    assert risk == 0.0


def test_size_unconfigured_risk_unit_is_zero() -> None:
    # risk_unit 0 (unconfigured) -> (0, 0.0): caller renders R-multiples, not a guessed $.
    shares, risk = size_order(
        conviction="high", entry_ceiling=12.0, stop=10.0, risk_unit_dollars=0.0)
    assert shares == 0
    assert risk == 0.0


def test_size_nonpositive_per_share_is_zero() -> None:
    # stop >= ceiling -> per-share risk <= 0 -> (0, 0.0), never a divide-by-zero.
    shares, risk = size_order(
        conviction="high", entry_ceiling=10.0, stop=10.0, risk_unit_dollars=250.0)
    assert shares == 0
    assert risk == 0.0


def test_size_max_shares_caps() -> None:
    # high would be 125 shares but max_shares=50 caps it; risk recomputes on the cap.
    shares, risk = size_order(
        conviction="high", entry_ceiling=12.0, stop=10.0, risk_unit_dollars=250.0,
        max_shares=50)
    assert shares == 50
    assert risk == 100.0

"""The conviction-calibration TEST -- the inferential teeth behind the autonomy gate.

``conviction_calibrated`` asks: does the analyst's ``high`` conviction OUT-EARN its
``low`` conviction, with TEETH? Not a bare ``mean_high > mean_low`` (the descriptive
Phase-2 ``analyst_calibration`` already does that), but a ticker-CLUSTERED two-sample
delta whose bootstrap lower bound must clear 0, gated behind an n + distinct-ticker
floor per bucket. The load-bearing test is the PLACEBO: a sample with no real high/low
signal (labels shuffled) must NOT pass, exactly as ``propose.py``'s label-shuffle placebo
keeps a coin-flip relabel from minting an edge.
"""

import random

from swing_screener.analytics.calibration import (
    _NUDGE_CEILING,
    CalibrationVerdict,
    conviction_calibrated,
    max_conviction_step,
)
from swing_screener.db.models import AnalystCall


def _verdict(*, calibrated: bool) -> CalibrationVerdict:
    """A minimal CalibrationVerdict carrying only the ``calibrated`` flag the bound reads."""
    return CalibrationVerdict(
        calibrated=calibrated, high_minus_low=0.4, ci_low=0.1,
        n_high=40, n_low=40, n_clusters_high=8, n_clusters_low=8, reason="x",
    )


def _call(ticker: str, conviction: str, r: float) -> AnalystCall:
    """A SCORED analyst call: ``scored_at`` + ``realized_r`` set so the test counts it.

    Only ``ticker`` (the cluster), ``final_conviction`` (the high/low split), and
    ``realized_r`` (the outcome) are load-bearing here; the rest are valid filler.
    """
    from datetime import date

    return AnalystCall(
        created_date=date(2026, 6, 1), ticker=ticker, timeframe="1d",
        play_type="continuation", run_date=date(2026, 6, 1),
        baseline_conviction="medium", final_conviction=conviction,
        nudge_reason="x", model="claude-opus-4-8",
        realized_r=r, scored_at=date(2026, 6, 5),
    )


def _group(convictions_rs: dict[str, list[float]], conviction: str) -> list[AnalystCall]:
    """Build scored calls for one conviction bucket: ``{ticker: [R, ...]}`` -> calls,
    a DISTINCT ticker per key so the clustered bootstrap sees real clusters."""
    return [
        _call(ticker, conviction, r)
        for ticker, rs in convictions_rs.items()
        for r in rs
    ]


# A clean, calibrated sample: high CLEARLY out-earns low (mean ~1.3R vs ~ -0.1R), both
# buckets are deep (n >= 20) across >= 8 distinct tickers, and the per-ticker means are
# tight, so the clustered two-sample delta lower bound sits well above 0.
_HIGH = {f"H{i}": [1.2, 1.4, 1.3, 1.2, 1.4] for i in range(8)}   # 40 calls, 8 tickers
_LOW = {f"L{i}": [-0.1, 0.0, -0.2, 0.1, -0.1] for i in range(8)}  # 40 calls, 8 tickers


def test_calibrated_when_high_clearly_outearns_low_across_clusters() -> None:
    calls = _group(_HIGH, "high") + _group(_LOW, "low")
    v = conviction_calibrated(calls)
    assert isinstance(v, CalibrationVerdict)
    assert v.calibrated is True
    assert v.ci_low > 0
    assert v.high_minus_low > 0
    assert v.n_high == 40 and v.n_low == 40
    assert v.n_clusters_high == 8 and v.n_clusters_low == 8


def test_placebo_shuffled_labels_does_not_pass() -> None:
    """THE TEETH (load-bearing): pool a single homogeneous R distribution across many
    tickers and randomly assign each call to ``high`` or ``low``. There is NO real high/low
    signal -- but with this seed the noise leaves ``mean_high`` ABOVE ``mean_low`` (a positive
    point-estimate gap), so a bare ``mean_high > mean_low`` check would WRONGLY pass. The
    clustered two-sample CI lower bound straddles 0, so ``conviction_calibrated`` correctly
    returns False. This test would FAIL if the check were bare means without the CI/clustering
    -- it is the analogue of ``propose.py``'s label-shuffle placebo (a relabel can't mint an
    edge).
    """
    rng = random.Random(0)
    # 16 tickers, 6 calls each, all drawn from ONE noisy distribution (no signal).
    calls: list[AnalystCall] = []
    for i in range(16):
        for _ in range(6):
            r = rng.gauss(0.3, 1.5)
            conviction = "high" if rng.random() < 0.5 else "low"
            calls.append(_call(f"T{i}", conviction, r))
    v = conviction_calibrated(calls)
    # The floors ARE met, so the verdict is decided by the CI gate, not the data floor.
    assert v.n_high >= 20 and v.n_low >= 20
    assert v.n_clusters_high >= 8 and v.n_clusters_low >= 8
    # The point-estimate gap is POSITIVE here: a bare mean_high>mean_low check would pass.
    assert v.high_minus_low > 0
    # But the clustered CI lower bound straddles 0 -> NOT certified. This is the teeth.
    assert v.calibrated is False
    assert v.ci_low <= 0


def test_below_n_floor_is_insufficient_data() -> None:
    # high has only 5 calls (< min_per_bucket=20) -> insufficient, regardless of the gap.
    high = {f"H{i}": [2.0] for i in range(5)}     # 5 calls, 5 tickers
    low = _LOW
    v = conviction_calibrated(_group(high, "high") + _group(low, "low"))
    assert v.calibrated is False
    assert v.reason.startswith("insufficient data")
    assert "n_high" in v.reason or "high" in v.reason


def test_below_cluster_floor_is_insufficient_data() -> None:
    # high has 40 calls but on only 4 distinct tickers (< cluster_floor=8) -> insufficient.
    high = {f"H{i}": [1.3] * 10 for i in range(4)}   # 40 calls, 4 tickers
    v = conviction_calibrated(_group(high, "high") + _group(_LOW, "low"))
    assert v.calibrated is False
    assert v.reason.startswith("insufficient data")


def test_high_not_beating_low_is_not_calibrated() -> None:
    # Deep, well-clustered samples, but high does NOT out-earn low (high mean < low mean):
    # the gap point estimate is negative, so the lower bound is too -> not calibrated.
    high = {f"H{i}": [0.0, 0.1, -0.1, 0.0, 0.1] for i in range(8)}   # mean ~0.02R
    low = {f"L{i}": [1.0, 1.2, 1.1, 1.0, 1.2] for i in range(8)}     # mean ~1.1R
    v = conviction_calibrated(_group(high, "high") + _group(low, "low"))
    assert v.calibrated is False
    assert v.high_minus_low < 0
    assert "out-earn" in v.reason or "ci_low" in v.reason


def test_ignores_unscored_calls() -> None:
    # An unscored high call (scored_at None) must not count toward the n floor.
    from datetime import date

    unscored = AnalystCall(
        created_date=date(2026, 6, 1), ticker="U0", timeframe="1d",
        play_type="continuation", run_date=date(2026, 6, 1),
        baseline_conviction="medium", final_conviction="high",
        nudge_reason="x", model="claude-opus-4-8", realized_r=None, scored_at=None,
    )
    calls = _group(_HIGH, "high") + _group(_LOW, "low") + [unscored]
    v = conviction_calibrated(calls)
    assert v.n_high == 40   # the unscored high call is excluded


def test_medium_and_avoid_convictions_are_excluded() -> None:
    # Only high vs low are tested; medium/avoid calls are ignored entirely.
    medium = {f"M{i}": [5.0] * 5 for i in range(8)}   # would dominate if counted
    calls = _group(_HIGH, "high") + _group(_LOW, "low") + _group(medium, "medium")
    v = conviction_calibrated(calls)
    assert v.n_high == 40 and v.n_low == 40   # medium excluded from both buckets


def test_deterministic_same_input_same_verdict() -> None:
    calls = _group(_HIGH, "high") + _group(_LOW, "low")
    a = conviction_calibrated(calls)
    b = conviction_calibrated(calls)
    assert a == b   # frozen dataclass + seeded bootstrap -> identical


# --- The earned nudge bound: ±2 ONLY when the play type certifies, capped at the ceiling ---

def test_max_conviction_step_calibrated_earns_two() -> None:
    # A certified play type earns the ±2 bound -- the analyst's influence GROWS (North Star #9).
    assert max_conviction_step(_verdict(calibrated=True)) == 2


def test_max_conviction_step_uncalibrated_stays_one() -> None:
    # An uncertified play type keeps today's hard ±1 clamp -- no earned bound yet.
    assert max_conviction_step(_verdict(calibrated=False)) == 1


def test_nudge_ceiling_constant_is_two() -> None:
    # The git-visible, bounded discipline: the ceiling is a named constant pinned at 2.
    assert _NUDGE_CEILING == 2


def test_max_conviction_step_never_exceeds_ceiling() -> None:
    # Even if a future caller passes a larger ceiling, min(ceiling, 2) caps the earned bound
    # at 2 -- the bound can never silently widen past the hard discipline constant.
    assert max_conviction_step(_verdict(calibrated=True), ceiling=5) == 2
    assert max_conviction_step(_verdict(calibrated=True), ceiling=2) == 2
    # An uncertified play type is always 1 regardless of the ceiling.
    assert max_conviction_step(_verdict(calibrated=False), ceiling=5) == 1

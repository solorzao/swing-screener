import statistics
from datetime import date

import pytest

from swing_screener.analytics.performance import (
    _clustered_ci_low,
    breakdown,
    equity_curve,
    rank_bucket,
    score_bucket,
    summarize,
)
from swing_screener.db.models import PaperTrade


def _pt(*, fill_status="filled", status="closed", realized_r=None, hold_bars=None,
        timeframe="1d", quality_tier="reputable", rank=1, exit_date=None, score=0.8,
        conviction_tier="base", opened_date=None):
    return PaperTrade(
        ticker="AAPL", timeframe=timeframe, horizon="medium", signal_score=score, rank=rank,
        mtf_aligned=True, quality_tier=quality_tier, volatility_tier="med",
        fill_status=fill_status, stop=95.0, target=110.0, risk=5.0, status=status,
        realized_r=realized_r, hold_bars=hold_bars, exit_date=exit_date,
        conviction_tier=conviction_tier, opened_date=opened_date,
    )


def test_size_weighted_expectancy_weights_by_conviction_tier():
    from swing_screener.analytics.performance import size_weighted_expectancy
    # premium R=+2 (weight 2.0), base R=-1 (weight 0.5): weighted = (2*2 + 0.5*-1)/2.5 = 1.4,
    # vs a plain mean of +0.5 -- sizing lifts the edge cohort and trims the dead one.
    trades = [_pt(realized_r=2.0, conviction_tier="premium"),
              _pt(realized_r=-1.0, conviction_tier="base")]
    exp, total_w = size_weighted_expectancy(trades)
    assert abs(exp - 1.4) < 1e-9
    assert abs(total_w - 2.5) < 1e-9
    # open / unfilled trades are ignored
    assert size_weighted_expectancy([_pt(status="open", realized_r=None)]) == (0.0, 0.0)


def test_tier_conditioned_stats_exclude_pre_stamping_rows():
    """conviction_tier was only COMPUTED from 2026-06-29; the migration backfilled every
    earlier row with the literal lowest tier 'base', so ~683 unknown-tier fills would
    masquerade as deliberate base-tier picks (and be size-weighted 0.5x), poisoning any
    tier/sizing statistic (2026-07 audit). Tier-conditioned aggregates exclude rows
    opened before the stamping date; undated rows (tests/fakes) fail open."""
    from swing_screener.analytics.performance import size_weighted_expectancy

    legacy = _pt(realized_r=-5.0, conviction_tier="base", opened_date=date(2026, 6, 20))
    stamped = _pt(realized_r=2.0, conviction_tier="premium", opened_date=date(2026, 7, 1))

    exp, total_w = size_weighted_expectancy([legacy, stamped])
    assert (exp, total_w) == (2.0, 2.0)  # the legacy row is excluded, not weighted 0.5x

    by_tier = breakdown([legacy, stamped], "conviction_tier")
    assert set(by_tier) == {"premium"}  # legacy 'base' masquerade excluded
    # NON-tier breakdowns are untouched -- the exclusion is tier-specific.
    assert breakdown([legacy, stamped], "timeframe")["1d"].n_total == 2


def _book():
    return [
        _pt(realized_r=2.0, hold_bars=5, rank=1, exit_date=date(2024, 1, 5)),
        _pt(realized_r=-1.0, hold_bars=3, rank=2, exit_date=date(2024, 1, 3)),
        _pt(realized_r=1.0, hold_bars=4, timeframe="1wk", quality_tier="mid", rank=3,
            exit_date=date(2024, 1, 8)),
        _pt(fill_status="missed", realized_r=None, rank=4),
        _pt(fill_status="invalidated", realized_r=None, rank=5),
        _pt(status="open", realized_r=None, hold_bars=2, rank=1),  # filled but still open
    ]


def test_summarize():
    s = summarize(_book())
    assert s.n_total == 6
    assert s.n_filled == 4                       # 3 closed filled + 1 open filled
    assert s.fill_rate == 4 / 6
    assert s.n_closed == 3                        # closed + filled + realized_r set
    assert s.win_rate == 2 / 3                    # 2 of 3 closed are winners
    assert abs(s.expectancy_r - (2.0 - 1.0 + 1.0) / 3) < 1e-9
    assert s.profit_factor == 3.0                 # (2+1) / abs(-1)
    assert s.avg_hold_bars == (5 + 3 + 4) / 3


def test_summarize_reports_win_and_loss_counts():
    # four closed trades: two winners, one loser, one scratch (R==0). A scratch is
    # NEITHER a win nor a loss (both use strict >/<), so n_wins + n_losses need not
    # equal n_closed.
    trades = [
        _pt(realized_r=1.0, exit_date=date(2024, 1, 1)),
        _pt(realized_r=0.5, exit_date=date(2024, 1, 2)),
        _pt(realized_r=-1.0, exit_date=date(2024, 1, 3)),
        _pt(realized_r=0.0, exit_date=date(2024, 1, 4)),
    ]
    s = summarize(trades)
    assert s.n_wins == 2
    assert s.n_losses == 1
    assert s.n_closed == 4  # the scratch is closed but is neither a win nor a loss


def test_summarize_empty():
    s = summarize([])
    assert s.n_total == 0 and s.fill_rate == 0.0 and s.win_rate == 0.0
    assert s.expectancy_stderr == 0.0
    assert s.expectancy_ci_low == 0.0 and s.expectancy_ci_high == 0.0


def test_expectancy_confidence_interval():
    import statistics
    s = summarize(_book())
    expected_se = statistics.stdev([2.0, -1.0, 1.0]) / (3 ** 0.5)
    assert abs(s.expectancy_stderr - expected_se) < 1e-9
    assert s.expectancy_ci_low < s.expectancy_r < s.expectancy_ci_high
    assert abs(s.expectancy_ci_low - (s.expectancy_r - 1.96 * expected_se)) < 1e-9


def test_thin_sample_interval_collapses_to_point():
    s = summarize([_pt(realized_r=1.5, hold_bars=2, exit_date=date(2024, 1, 1))])
    assert s.n_closed == 1
    assert s.expectancy_stderr == 0.0
    assert s.expectancy_ci_low == s.expectancy_r == s.expectancy_ci_high


def test_lower_bound_penalises_thin_noisy_samples():
    # identical point estimate (~1.0R), but the large tight sample earns a higher LOWER
    # bound than the tiny noisy one -- so ranking by ci_low never crowns the noise.
    tight = [_pt(realized_r=r, hold_bars=1, exit_date=date(2024, 1, 1))
             for r in (0.9, 1.1, 0.9, 1.1, 1.0, 1.0, 0.9, 1.1)]
    noisy = [_pt(realized_r=r, hold_bars=1, exit_date=date(2024, 1, 1)) for r in (-2.0, 4.0)]
    assert abs(summarize(tight).expectancy_r - summarize(noisy).expectancy_r) < 1e-9
    assert summarize(tight).expectancy_ci_low > summarize(noisy).expectancy_ci_low


def test_score_bucket_labels_and_assignment():
    trades = [
        _pt(score=0.45, realized_r=-1.0, exit_date=date(2024, 1, 1)),  # 0.00-0.50
        _pt(score=0.55, realized_r=0.0, exit_date=date(2024, 1, 2)),   # 0.50-0.60
        _pt(score=0.85, realized_r=2.0, exit_date=date(2024, 1, 3)),   # 0.80-1.00
        _pt(score=0.80, realized_r=1.0, exit_date=date(2024, 1, 4)),   # edge -> higher band
    ]
    b = score_bucket(trades, [0.5, 0.6, 0.7, 0.8])
    assert list(b) == ["0.00-0.50", "0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-1.00"]
    assert b["0.00-0.50"].n_closed == 1 and b["0.00-0.50"].expectancy_r == -1.0
    assert b["0.60-0.70"].n_closed == 0                       # empty band still present
    assert b["0.80-1.00"].n_closed == 2                       # 0.85 and the on-edge 0.80
    assert b["0.80-1.00"].expectancy_r == 1.5                 # (2.0 + 1.0) / 2


def test_score_bucket_surfaces_a_calibrated_score():
    # higher score bands earn more -> the score separates winners from losers
    trades = (
        [_pt(score=0.45, realized_r=-1.0, exit_date=date(2024, 1, 1)) for _ in range(3)]
        + [_pt(score=0.85, realized_r=2.0, exit_date=date(2024, 1, 2)) for _ in range(3)]
    )
    b = score_bucket(trades, [0.5, 0.6, 0.7, 0.8])
    assert b["0.80-1.00"].expectancy_r > b["0.00-0.50"].expectancy_r


def test_breakdown_by_timeframe():
    b = breakdown(_book(), "timeframe")
    assert set(b.keys()) == {"1d", "1wk"}
    assert b["1wk"].n_closed == 1 and b["1wk"].win_rate == 1.0


def test_rank_bucket():
    rb = rank_bucket(_book(), [2])  # buckets: "1-2", "3+"
    assert set(rb.keys()) == {"1-2", "3+"}
    assert rb["1-2"].n_total == 3   # ranks 1, 2, 1
    assert rb["3+"].n_total == 3    # ranks 3, 4, 5


def test_equity_curve_cumulative_in_exit_date_order():
    curve = equity_curve(_book())
    assert curve == [
        (date(2024, 1, 3), -1.0),
        (date(2024, 1, 5), 1.0),
        (date(2024, 1, 8), 2.0),
    ]


# --- ticker-clustered bootstrap lower bound -------------------------------------------

def _ct(ticker, realized_r):
    """A minimal closed-filled trade on a named ticker, for clustering tests."""
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        mtf_aligned=True, quality_tier="reputable", volatility_tier="med",
        fill_status="filled", stop=95.0, target=110.0, risk=5.0, status="closed",
        realized_r=realized_r, hold_bars=3, exit_date=date(2024, 1, 1),
    )


def _iid_low(realized):
    """The old IID normal-approximation lower bound, recomputed independently."""
    mean = sum(realized) / len(realized)
    se = statistics.stdev(realized) / (len(realized) ** 0.5) if len(realized) >= 2 else 0.0
    return mean - 1.96 * se


def test_clustered_bound_materially_below_iid_when_one_ticker_dominates():
    # 18 strongly-positive trades all on "AAA" + 7 tiny trades spread one-per-ticker.
    # The IID bound treats all 25 as independent and reads optimistic; the clustered
    # bootstrap resamples whole tickers, so resamples that omit "AAA" collapse the mean
    # -> the 2.5th percentile sits FAR below the IID lower bound.
    realized = [2.0] * 18 + [0.05] * 7
    trades = [_ct("AAA", 2.0) for _ in range(18)]
    trades += [_ct(f"T{i}", 0.05) for i in range(7)]  # 8 distinct tickers, clears the floor
    s = summarize(trades)

    iid_low = _iid_low(realized)
    assert s.n_clusters == 8 and s.thin_clusters is False
    # "materially below": at least a full 0.5R gap, not a rounding wobble.
    assert s.expectancy_ci_low < iid_low - 0.5
    # sanity on the magnitude: the clustered bound is dragged toward the tiny-ticker world.
    assert s.expectancy_ci_low < 0.5


def test_clustered_bound_is_deterministic_for_fixed_seed():
    trades = [_ct("AAA", 2.0) for _ in range(18)] + [_ct(f"T{i}", 0.05) for i in range(7)]
    first = summarize(trades).expectancy_ci_low
    second = summarize(trades).expectancy_ci_low
    assert first == second  # identical, not merely close -- a fixed bootstrap seed


def test_clustered_bound_falls_back_to_iid_below_distinct_ticker_floor():
    # Only 3 distinct tickers (< the floor): too few clusters to bootstrap, so the bound
    # falls back to the IID estimate and is flagged thin.
    realized = [2.0, 2.0, 2.0, 0.05, 0.05, 1.0, 1.0]
    trades = (
        [_ct("AAA", 2.0) for _ in range(3)]
        + [_ct("BBB", 0.05) for _ in range(2)]
        + [_ct("CCC", 1.0) for _ in range(2)]
    )
    s = summarize(trades)
    assert s.n_clusters == 3 and s.thin_clusters is True
    assert abs(s.expectancy_ci_low - _iid_low(realized)) < 1e-9


def _dispersed_by_ticker():
    """>= the cluster floor (8) distinct tickers with heavy dispersion, so a stricter
    percentile lands materially below the 2.5% one. One ticker is strongly negative and
    one strongly positive; the rest spread between -- resamples that load up on the
    extreme negative ticker drag the deep-tail (0.5%) percentile well below the 2.5%."""
    return {
        "AAA": [-20.0, -20.0, -20.0, -20.0],
        "BBB": [-3.0, -3.0],
        "CCC": [-1.0],
        "DDD": [0.0],
        "EEE": [0.5],
        "FFF": [1.0, 1.0],
        "GGG": [3.0],
        "HHH": [6.0, 6.0, 6.0],
    }


def test_clustered_ci_low_stricter_percentile_gives_lower_bound():
    # A stricter (deeper-tail) percentile -> a lower, more conservative bound on the SAME
    # multi-ticker data. iid_low is a large positive sentinel so min() never clips either
    # clustered value -- both bounds come straight from the bootstrap percentile.
    by_ticker = _dispersed_by_ticker()
    sentinel = 1e9
    low_2_5, n_clusters, thin = _clustered_ci_low(by_ticker, sentinel)
    low_0_5, n2, thin2 = _clustered_ci_low(by_ticker, sentinel, lower_pct=0.5)
    assert n_clusters == n2 == 8 and thin is thin2 is False
    # genuine, deterministic magnitude: the 0.5% bound is at least a full 0.5R lower.
    assert low_0_5 <= low_2_5 - 0.5


def test_clustered_ci_low_default_pct_is_byte_identical_to_explicit_2_5():
    # Omitting lower_pct must be IDENTICAL (not merely close) to passing 2.5 -- the
    # default path is the Phase-0 behavior, unchanged to the bit.
    by_ticker = _dispersed_by_ticker()
    sentinel = 1e9
    assert _clustered_ci_low(by_ticker, sentinel) == _clustered_ci_low(
        by_ticker, sentinel, lower_pct=2.5
    )


def test_clustered_bound_never_above_iid_across_varied_inputs():
    # The min(iid_low, clustered_low) rule: on ANY input the reported lower bound is
    # never MORE optimistic than the IID bound, whether clustering fires or falls back.
    cases = [
        # many tickers, mixed signs -> clustering fires
        [_ct(f"T{i}", r) for i, r in enumerate(
            [3.0, -1.0, 2.0, -2.0, 1.5, 0.5, -0.5, 4.0, -3.0, 1.0])],
        # one dominant ticker + spread tail -> clustering fires, widens hard
        ([_ct("AAA", 2.0) for _ in range(18)] + [_ct(f"T{i}", -0.2) for i in range(7)]),
        # below the floor -> falls back to IID (must be EQUAL, the boundary of the min)
        [_ct("AAA", 1.0), _ct("BBB", -1.0), _ct("CCC", 2.0)],
        # all-identical, many tickers -> bootstrap mean is constant, equals IID
        [_ct(f"T{i}", 1.0) for i in range(12)],
    ]
    for trades in cases:
        realized = [t.realized_r for t in trades]
        s = summarize(trades)
        assert s.expectancy_ci_low <= _iid_low(realized) + 1e-9


def test_score_stamped_excludes_pre_v2_reversal_rows():
    from swing_screener.analytics.performance import SCORE_STAMPED_FROM, score_stamped

    cutoff = SCORE_STAMPED_FROM["reversal"]
    old_rev = _pt(realized_r=1.0, opened_date=date(2026, 6, 1))
    old_rev.play_type = "reversal"
    new_rev = _pt(realized_r=1.0, opened_date=cutoff)
    new_rev.play_type = "reversal"
    old_cont = _pt(realized_r=1.0, opened_date=date(2026, 6, 1))  # continuation: unchanged
    undated_rev = _pt(realized_r=1.0)                             # tests/fakes: fail open
    undated_rev.play_type = "reversal"

    kept = score_stamped([old_rev, new_rev, old_cont, undated_rev])
    # the pre-v2 reversal row measured a DIFFERENT score definition -> excluded;
    # everything else (post-cutoff, other play types, undated) stays.
    assert old_rev not in kept
    assert new_rev in kept and old_cont in kept and undated_rev in kept


def _arm_pair(ticker: str, ts_day: int, base_r: float, arm_r: float) -> list[PaperTrade]:
    """A closed baseline/be_1r twin sharing one fill identity, for paired-delta tests."""
    from datetime import datetime

    ts = datetime(2026, 6, ts_day)
    a = _pt(realized_r=base_r, opened_date=date(2026, 6, ts_day))
    a.ticker, a.arm, a.trigger_ts = ticker, "baseline", ts
    b = _pt(realized_r=arm_r, opened_date=date(2026, 6, ts_day))
    b.ticker, b.arm, b.trigger_ts = ticker, "be_1r", ts
    return [a, b]


def test_paired_arm_delta_pairs_on_fill_identity():
    from datetime import datetime

    from swing_screener.analytics.performance import paired_arm_delta

    trades = (_arm_pair("AAA", 1, 1.0, 1.5) + _arm_pair("AAA", 2, -1.0, 0.0)
              + _arm_pair("BBB", 3, 0.5, 0.5))
    # an UNPAIRED arm row (no baseline twin) and a pair with an open leg must be skipped
    lone = _pt(realized_r=2.0)
    lone.ticker, lone.arm, lone.trigger_ts = "CCC", "be_1r", datetime(2026, 6, 4)
    open_base = _pt(status="open", realized_r=None)
    open_base.ticker, open_base.arm, open_base.trigger_ts = "DDD", "baseline", datetime(2026, 6, 5)
    open_arm = _pt(realized_r=1.0)
    open_arm.ticker, open_arm.arm, open_arm.trigger_ts = "DDD", "be_1r", datetime(2026, 6, 5)

    d = paired_arm_delta(trades + [lone, open_base, open_arm], "be_1r")
    assert d.n_pairs == 3
    assert abs(d.mean_delta - (0.5 + 1.0 + 0.0) / 3) < 1e-9
    assert d.n_clusters == 2  # AAA + BBB
    # empty input fails closed
    empty = paired_arm_delta([], "be_1r")
    assert empty.n_pairs == 0 and empty.thin_clusters is True


def test_paired_arm_delta_carries_upper_bound_and_stderr() -> None:
    # The settlement card's futility check needs the UPPER bound and the raw stderr.
    # Only the LOWER bound is hardened by the clustered bootstrap; the upper stays the
    # plain IID normal approximation (mean + 1.96 * stderr) and is labeled as such.
    from swing_screener.analytics.performance import paired_arm_delta

    trades = (_arm_pair("AAA", 1, 1.0, 1.5) + _arm_pair("AAA", 2, -1.0, 0.0)
              + _arm_pair("BBB", 3, 0.5, 0.5))
    d = paired_arm_delta(trades, "be_1r")
    assert isinstance(d.stderr, float) and isinstance(d.delta_ci_high, float)
    # deltas are (0.5, 1.0, 0.0): stderr = stdev / sqrt(n), upper = mean + 1.96 * stderr
    expected_se = statistics.stdev([0.5, 1.0, 0.0]) / (3 ** 0.5)
    assert abs(d.stderr - expected_se) < 1e-9
    assert abs(d.delta_ci_high - (d.mean_delta + 1.96 * d.stderr)) < 1e-9
    assert d.delta_ci_low < d.mean_delta < d.delta_ci_high

    # a single pair has no computable stderr -> the interval collapses to the point
    single = paired_arm_delta(_arm_pair("AAA", 1, 1.0, 1.5), "be_1r")
    assert single.stderr == 0.0 and single.delta_ci_high == single.mean_delta
    # empty input fails closed with zeros
    empty = paired_arm_delta([], "be_1r")
    assert empty.stderr == 0.0 and empty.delta_ci_high == 0.0


def test_trailing_expectancy_windows_by_exit_date() -> None:
    from datetime import timedelta

    from swing_screener.analytics.performance import trailing_expectancy

    # 30 closed trades with staggered exit_dates; trades 14 and 15 close on the SAME day
    # (a two-close date must collapse to one point carrying the LAST trailing value).
    base = date(2026, 1, 1)
    days = list(range(30))
    days[15] = 14
    trades = [_pt(realized_r=float(i), exit_date=base + timedelta(days=d))
              for i, d in enumerate(days)]

    curve = trailing_expectancy(trades, window=10)

    # one point per DATE, ascending (30 closes -> 29 points: the shared day collapses)
    assert [d for d, _ in curve] == sorted({base + timedelta(days=d) for d in days})
    # each point = mean realized_r of the trailing 10 closes in exit_date order; with
    # fewer than 10 closes so far, the mean of what exists. Same-date closes collapse
    # to the value AFTER the last close of that date.
    values = [float(i) for i in range(30)]
    expected: dict[date, float] = {}
    for k, d in enumerate(days):
        tail = values[max(0, k - 9):k + 1]
        expected[base + timedelta(days=d)] = sum(tail) / len(tail)
    for point_date, point_value in curve:
        assert abs(point_value - expected[point_date]) < 1e-9

    # equity_curve's filtering rules: open / unfilled / dateless rows contribute nothing
    noise = [_pt(status="open", realized_r=None),
             _pt(fill_status="missed", realized_r=None),
             _pt(realized_r=9.0)]  # closed but no exit_date
    assert trailing_expectancy(trades + noise, window=10) == curve
    assert trailing_expectancy([], window=10) == []

    # deterministic under caller iteration order: same-date closes are ordered by row id,
    # so a same-date block straddling the window boundary cannot shuffle the mean
    same_day: list[PaperTrade] = []
    for i in range(12):
        t = _pt(realized_r=float(i), exit_date=base)
        t.id = i + 1
        same_day.append(t)
    expected_point = [(base, sum(range(2, 12)) / 10)]  # last 10 by id carry values 2..11
    assert trailing_expectancy(same_day, window=10) == expected_point
    assert trailing_expectancy(list(reversed(same_day)), window=10) == expected_point

    # window is a count of closes; zero or negative is a caller bug, not all-history
    with pytest.raises(ValueError):
        trailing_expectancy(trades, window=0)


def test_cost_stamped_from_gates_cost_level() -> None:
    from datetime import timedelta

    from swing_screener.analytics.performance import COST_STAMPED_FROM, cost_level_for

    after = COST_STAMPED_FROM
    before = COST_STAMPED_FROM - timedelta(days=1)

    # every closed trade provably exited on/after the 0.05 default shipping -> "0.05"
    net = [_pt(realized_r=1.0, exit_date=after),
           _pt(realized_r=-0.5, exit_date=after)]
    assert cost_level_for(net) == "0.05"
    # open / unfilled rows carry no realized cost yet and do not block the stamp
    assert cost_level_for(net + [_pt(status="open", realized_r=None)]) == "0.05"

    # ONE pre-cutoff exit poisons the aggregate (gross rows carry no per-row marker)
    assert cost_level_for(net + [_pt(realized_r=1.0, exit_date=before)]) is None
    # a closed trade with NO exit_date cannot prove its cost level -> None
    assert cost_level_for(net + [_pt(realized_r=1.0)]) is None
    # empty -> None
    assert cost_level_for([]) is None

    # a STRADDLER -- partialed before the cutoff (gross partial leg, priced at partial
    # time), exited after -- carries a gross component blended into realized_r; no
    # partial date is persisted, so only opened_date >= cutoff proves the partial's vintage
    straddler = _pt(realized_r=1.0, exit_date=after, opened_date=before)
    straddler.partial_done = True
    assert cost_level_for(net + [straddler]) is None
    # a partial on a trade OPENED on/after the cutoff can only have happened after it
    proven = _pt(realized_r=1.0, exit_date=after, opened_date=after)
    proven.partial_done = True
    assert cost_level_for(net + [proven]) == "0.05"
    # a partialed trade with no opened_date cannot prove its partial's vintage -> None
    undated_partial = _pt(realized_r=1.0, exit_date=after)
    undated_partial.partial_done = True
    assert cost_level_for(net + [undated_partial]) is None

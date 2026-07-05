"""The Stat contract: no statistic leaves the API as a bare number.

North Star #2 as a type: a Stat always carries n, clusters, both CI bounds, its cost
level, and its corpus id. Unknown provenance is an explicit None (the UI renders a
hollow 'not measured' tick), never a silent default.
"""

from swing_screener.analytics.performance import summarize
from swing_screener.cockpit.stats import Stat, stat_from_summary
from swing_screener.db.models import PaperTrade


def _trade(ticker: str, r: float) -> PaperTrade:
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        fill_status="filled", stop=95.0, target=110.0, risk=5.0, status="closed",
        realized_r=r,
    )


def test_stat_carries_full_provenance() -> None:
    s = Stat(value=0.057, n=9697, n_clusters=511, ci_low=0.028, ci_high=0.086,
             cost_level="0.05", corpus_id="pinned-20260703", facet="research",
             unit="R", thin=False)
    d = s.as_dict()
    for key in ("value", "n", "n_clusters", "ci_low", "ci_high", "cost_level",
                "corpus_id", "facet", "unit", "thin"):
        assert key in d


def test_stat_from_summary_maps_performance_fields() -> None:
    trades = [_trade(t, r) for t, r in
              [("AAA", 1.0), ("AAA", -1.0), ("BBB", 0.5), ("CCC", -0.2),
               ("DDD", 0.3), ("EEE", 0.1), ("FFF", -0.4), ("GGG", 0.8),
               ("HHH", 0.2), ("III", -0.1)]]
    summary = summarize(trades)
    s = stat_from_summary(summary, cost_level=None, corpus_id=None, facet="research")
    assert s.value == summary.expectancy_r
    assert s.n == summary.n_closed
    assert s.n_clusters == summary.n_clusters
    assert s.ci_low == summary.expectancy_ci_low
    assert s.ci_high == summary.expectancy_ci_high
    assert s.cost_level is None          # honest unknown, not a guessed default
    assert s.corpus_id is None
    assert s.facet == "research"
    assert s.unit == "R"                 # the default unit
    assert s.thin == summary.thin_clusters

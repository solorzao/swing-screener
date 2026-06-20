from swing_screener.analytics.significance import ArmVerdict
from swing_screener.dashboard.ui import format_arm_verdict


def _v(verdict, **kw):
    base = dict(arm="partial33_cond", baseline="baseline", n_pairs=40, n_clusters=12,
                mean_diff_r=0.08, ci_low=0.06, ci_high=0.10, margin_r=0.05, alpha=0.05,
                family_size=2, verdict=verdict, detail="d")
    base.update(kw)
    return ArmVerdict(**base)


def test_winner_is_flagged_clearly():
    out = format_arm_verdict(_v("winner"))
    assert "✅" in out and "+0.08R" in out and "partial33_cond" in out


def test_insufficient_data_shows_counts_not_a_false_winner():
    out = format_arm_verdict(_v("insufficient_data", n_pairs=4, n_clusters=2,
                                ci_low=float("nan"), ci_high=float("nan")))
    assert "insufficient" in out.lower() and "4" in out
    assert "✅" not in out

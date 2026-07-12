from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade
from swing_screener.db.session import get_engine
from swing_screener.options.stats import lab_summary


def _closed(day: int, r: float, grade_setup=None, **kw) -> OptionPaperTrade:
    opened = datetime(2026, 7, 1, 10, 0) + timedelta(days=day)
    base = dict(account="options-lab", strategy="gex", underlying="SPY", direction="long",
                opened_at=opened, closed_at=opened + timedelta(hours=1),
                entry=100.0, stop=99.0, target=102.0, status="closed", realized_r=r)
    base.update(kw)
    return OptionPaperTrade(**base)


def test_lab_summary_clusters_by_session() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        # 3 sessions x 2 trades
        for day in range(3):
            s.add(_closed(day, 1.0))
            s.add(_closed(day, -0.5))
        s.commit()
        stat = lab_summary(s, account="options-lab")
    assert stat["n"] == 6
    assert stat["n_clusters"] == 3          # sessions, not tickers
    assert stat["unit"] == "R"
    assert stat["facet"] == "gex-lab"
    assert set(stat) >= {"value", "n", "n_clusters", "ci_low", "ci_high",
                         "cost_level", "corpus_id", "facet", "unit", "thin_clusters"}


def test_robinhood_book_is_never_pooled() -> None:
    # Imported episodes carry premium_pnl, NOT realized_r (commit_episodes never
    # sets it) — seed them the way the importer actually writes them.
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_closed(0, None, account="robinhood", import_key="k1",
                      realized_r=None, premium_pnl=250.0))
        s.add(_closed(1, 1.0))
        s.commit()
        stat = lab_summary(s, account="options-lab")
    assert stat["n"] == 1


def test_robinhood_summary_reads_premium_pnl_as_plain_values() -> None:
    from swing_screener.options.stats import robinhood_summary

    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(_closed(0, None, account="robinhood", strategy="gex", import_key="k1",
                      realized_r=None, premium_pnl=250.0))
        s.add(_closed(1, None, account="robinhood", strategy="other", import_key="k2",
                      realized_r=None, premium_pnl=-80.0))
        s.commit()
        summary = robinhood_summary(s)
    assert summary["gex"] == {"n": 1, "total_pnl": 250.0, "wins": 1, "losses": 0, "open": 0}
    assert summary["other"]["total_pnl"] == -80.0
    # Plain labeled values by design — no CI keys; the premium book is a display,
    # not pooled inference (docs/modules/gex-lab.md).
    assert "ci_low" not in summary["gex"]

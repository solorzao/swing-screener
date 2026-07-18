"""Auditor anomaly grader: drought, would-surface leaks, calibration, orphan paper
exits -- and the zero-leak guarantee that a manual close / robinhood row is invisible
to the machine audit."""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db.models import (
    ExitEvent,
    OptionPaperTrade,
    PaperTrade,
    ReversalFunnel,
)
from swing_screener.db.session import get_engine
from swing_screener.journal.audit_anomaly import anomaly_findings

_FROM, _TO = date(2026, 7, 6), date(2026, 7, 12)


def _funnel(run_date, *, detected, confirmed, fresh, actionable, surfaced, overflow=""):
    return ReversalFunnel(run_date=run_date, detected=detected, confirmed=confirmed,
                          fresh=fresh, actionable=actionable, surfaced=surfaced,
                          overflow_tickers=overflow)


def test_drought_and_would_surface_leaks():
    # No overflow recorded on either day, so every actionable-vs-surfaced gap is
    # UNEXPLAINED -- these are the genuine leaks and still count.
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([
            _funnel(date(2026, 7, 7), detected=5, confirmed=3, fresh=2, actionable=2, surfaced=0),
            _funnel(date(2026, 7, 8), detected=4, confirmed=2, fresh=2, actionable=2, surfaced=1),
        ])
        s.commit()
        f = anomaly_findings(s, period_from=_FROM, period_to=_TO)
        assert f.drought_days == 1                 # 7/7: detected 5 but surfaced 0
        assert f.would_surface_leaks == 3          # (2-0) + (2-1), none explained


def test_routine_top5_sector_overflow_is_not_a_leak():
    """Picks that only lost the top-5/sector race are recorded per-name in
    overflow_tickers -- routine machinery, not an anomaly. Counting them fired the
    leak alarm on every busy week and eroded the $0 dead-week gate."""
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([
            # fully explained: gap of 3, all 3 named in the overflow line -> 0 leaks
            _funnel(date(2026, 7, 7), detected=9, confirmed=9, fresh=8, actionable=8,
                    surfaced=5, overflow="CRM,WDAY,PTC"),
            # partially explained: gap of 3, only 2 recorded -> 1 genuine leak
            _funnel(date(2026, 7, 8), detected=9, confirmed=9, fresh=8, actionable=8,
                    surfaced=5, overflow="CRM,WDAY"),
        ])
        s.commit()
        f = anomaly_findings(s, period_from=_FROM, period_to=_TO)
        assert f.would_surface_leaks == 1
        assert f.drought_days == 0


def test_orphan_paper_exit_is_flagged():
    with Session(get_engine("sqlite:///:memory:")) as s:
        pt = PaperTrade(ticker="AMD", timeframe="1d", horizon="medium", signal_score=0.8,
                        rank=1, account="research", fill_status="filled", stop=95.0,
                        target=110.0, risk=5.0, status="closed", realized_r=1.0)
        s.add(pt)
        s.commit()
        s.add_all([
            ExitEvent(created_date=date(2026, 7, 8), is_paper=True, trade_id=pt.id,
                      tier="", reason="target", account="research"),
            ExitEvent(created_date=date(2026, 7, 8), is_paper=True, trade_id=999,
                      tier="", reason="target", account="research"),  # orphan
        ])
        s.commit()
        assert anomaly_findings(s, period_from=_FROM, period_to=_TO).orphan_exit_events == 1


def test_zero_leak_manual_close_and_robinhood_are_invisible():
    """A manual equity close (is_paper=False) and a robinhood option must NOT enter the
    machine audit -- the Auditor filters ExitEvent by is_paper, never by account, and
    never reads OptionPaperTrade."""
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([
            # a human close wears account="research" but is_paper=False -> excluded
            ExitEvent(created_date=date(2026, 7, 8), is_paper=False, trade_id=1,
                      tier="", reason="manual_close", account="research"),
            OptionPaperTrade(account="robinhood", strategy="gex", underlying="SPY",
                             direction="long", premium_pnl=42.0, status="closed"),
        ])
        s.commit()
        f = anomaly_findings(s, period_from=_FROM, period_to=_TO)
        # the manual close is is_paper=False -> not counted as an orphan paper exit
        assert f.orphan_exit_events == 0
        assert f.drought_days == 0 and f.would_surface_leaks == 0
